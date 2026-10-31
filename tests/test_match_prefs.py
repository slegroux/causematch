"""The deck under a volunteer's preferences: places (and, as they land, when and work)."""

import importlib
import json
from datetime import datetime, timedelta

import pytest
from conftest import close_all_openings, coordinator, make_opening, uid_of

from app import data, geo, match
from app.db import (
    causes,
    db,
    deck_empties,
    matches,
    orgs,
    places,
    profiles,
    shifts,
)
from app.metrics import pilot_metrics

HOME_PLACES = (
    True  # each test runs with a saved Home place and as a profile from before
)

cause_prefs_migration = importlib.import_module("app.migrations.0010_cause_prefs")
day_slots_migration = importlib.import_module("app.migrations.0011_day_slots")

DOWNTOWN = geo.neighborhood_latlon("Downtown")[1]
WEST_LA = geo.neighborhood_latlon("West Los Angeles")[1]
PASADENA = geo.neighborhood_latlon("Pasadena")[1]
BASE = {
    "cause_food-insecurity": "love",
    "availability": ["sat_morning"],
    "frequency": "weekly",
    "only_distance": "only",  # the default: a place's travel time is a limit
    "sections": ["when", "work", "causes"],  # as the page sends: its checkboxes count
}


def add(title, where, starts_at="2099-01-03T09:00", **kw):
    lat, lon = where
    return make_opening(
        title,
        **{
            "causes": ["food-insecurity"],
            "lat": lat,
            "lon": lon,
            "neighborhood": "Somewhere",
            "starts_at": starts_at,
            **kw,
        },
    ).id


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
    close_all_openings()
    return user


# The places a `user` starts with: onboarding's Home (widened to any distance), or none
AT_START = {"home place": [("Home", "90012", "car", 0)], "sign-up point": []}


def places_of(client):
    return [
        (p.label, p.area, p.mode, p.max_minutes)
        for p in places(where="user_id = ?", where_args=[uid_of(client)], order_by="id")
    ]


def from_before_places(client):
    """Make the volunteer a profile from before places: no row, the sign-up point is Home."""
    places.delete_where("user_id = ?", [uid_of(client)])


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
    # the card's place line says it
    assert f"{top.split(' from ')[0]} · " in bare.get("/deck").text


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
    page = bare.get("/me/preferences/causes").text
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
    assert places_of(bare) == AT_START[bare.home]  # nothing saved


def test_place_a_zip_and_a_neighborhood_resolve(bare):
    save(bare, place_fields(1, "Home", "90066"), place_fields(2, "Gym", "silverlake"))
    home, gym = places(where="user_id = ?", where_args=[uid_of(bare)], order_by="id")
    assert (home.area, (home.lat, home.lon)) == ("90066", geo.zip_latlon("90066"))
    assert (gym.label, gym.area) == ("Gym", "Silverlake")


def test_place_an_unknown_name_saves_nothing(bare):
    r = save(bare, place_fields(1, "Home", "Atlantis"))
    assert r.status_code == 200 and "couldn't find “Atlantis”" in r.text
    assert 'value="Atlantis"' in r.text  # what they typed stays in the field
    assert places_of(bare) == AT_START[bare.home]  # nothing saved


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
    assert places_of(bare) == AT_START[bare.home]  # nothing saved


def test_place_the_main_place_is_required_and_names_are_short(bare):
    r = save(bare, place_fields(1, "Home", ""), place_fields(2, "Work", "Venice"))
    assert "Tell us your main place" in r.text
    r = save(bare, place_fields(1, "x" * 21, "Venice"))
    assert "up to 20 characters" in r.text
    assert places_of(bare) == AT_START[bare.home]  # nothing saved


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
    from_before_places(bare)
    profiles.update(id=uid_of(bare), radius_km=16)
    page = bare.get("/me/preferences/causes").text
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
    assert "geolocation=(self)" in policy("/me/preferences/causes")
    assert "geolocation=()" in policy("/deck")
    assert "geolocation=()" in policy("/profile")


def test_place_page_lists_neighborhoods_and_loads_geo_js(bare):
    page = bare.get("/me/preferences/causes").text
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


# --- when ----------------------------------------------------------------------------------

LA = match.LA
FMT = "%Y-%m-%dT%H:%M"


def ahead(days=0, hours=0, at=None):
    """A local wall-clock start this far ahead; at="18:00" pins the time of day."""
    t = datetime.now(LA) + timedelta(days=days, hours=hours)
    if at:
        h, m = map(int, at.split(":"))
        t = t.replace(hour=h, minute=m)
    return t.strftime(FMT)


def next_weekday(weekday, at, weeks=1):
    """The given weekday (0 = Monday) at least a week ahead, at a time of day."""
    t = datetime.now(LA) + timedelta(weeks=weeks)
    t += timedelta(days=(weekday - t.weekday()) % 7)
    return ahead(days=(t.date() - datetime.now(LA).date()).days, at=at)


def when_form(**over):
    return {
        **place_fields(1, "Home", "Downtown", minutes="0"),
        "horizon_days": "0",
        "commitment": "either",
        **over,
    }


def test_when_a_tuesday_evening_volunteer_sees_tuesday_first(bare):
    save(bare, when_form(availability=["tue_evening"]))
    add("wednesday 6 pm", DOWNTOWN, starts_at=next_weekday(2, "18:00"))
    add("tuesday 6 pm", DOWNTOWN, starts_at=next_weekday(1, "18:00", weeks=2))
    assert list(cards(bare)) == ["tuesday 6 pm", "wednesday 6 pm"]


def test_when_a_free_date_matches_a_one_off_shift_that_day(bare):
    day = next_weekday(3, "10:00", weeks=2)  # a Thursday: not in the grid below
    save(bare, when_form(availability=["sat_morning"], free_dates=[day[:10]]))
    add("an earlier thursday", DOWNTOWN, starts_at=next_weekday(3, "10:00"))
    add("that thursday", DOWNTOWN, starts_at=day)
    # later, so only the free date can lift it
    assert list(cards(bare)) == ["that thursday", "an earlier thursday"]
    assert json.loads(profiles[uid_of(bare)].free_dates) == [day[:10]]


def test_when_horizon_7_hides_a_shift_10_days_out(bare):
    save(bare, when_form(horizon_days="7"))
    add("in 3 days", DOWNTOWN, starts_at=ahead(days=3, at="10:00"))
    far = add("in 10 days", DOWNTOWN, starts_at=ahead(days=10, at="10:00"))
    assert list(cards(bare)) == ["in 3 days"]
    with pytest.raises(LookupError):
        data.swipe(uid_of(bare), far, True)
    save(bare, when_form(horizon_days="0"))
    assert set(cards(bare)) == {"in 3 days", "in 10 days"}


def test_when_free_today_shows_the_next_48_hours_soonest_first(bare):
    save(bare, when_form(availability=["sat_morning"]))
    add("in 3 days", DOWNTOWN, starts_at=ahead(days=3))
    add("in 20 hours", DOWNTOWN, starts_at=ahead(hours=20))
    add("in 5 hours", DOWNTOWN, starts_at=ahead(hours=5))
    today = [c["title"] for c in data.deck(uid_of(bare), 50, today=True)]
    assert today == ["in 5 hours", "in 20 hours"]
    assert "in 3 days" in cards(bare)  # the switch off: everything again
    page = bare.get("/deck?today=1").text
    assert "in 5 hours" in page and 'aria-pressed="true"' in page
    assert 'value="1" name="today"' in page  # the swipe form keeps the switch on
    assert 'aria-pressed="false"' in bare.get("/deck").text


def test_when_free_today_says_how_many_shifts_it_found(bare):
    save(bare, when_form())
    add("in 5 hours", DOWNTOWN, starts_at=ahead(hours=5))
    assert "1 shift in the next 48 hours" in bare.get("/deck?today=1").text
    add("in 20 hours", DOWNTOWN, starts_at=ahead(hours=20))
    assert "2 shifts in the next 48 hours" in bare.get("/deck?today=1").text
    assert "in the next 48 hours" not in bare.get("/deck").text


def test_when_free_today_survives_a_swipe(bare):
    save(bare, when_form())
    first = add("in 2 hours", DOWNTOWN, starts_at=ahead(hours=2))
    add("in 30 hours", DOWNTOWN, starts_at=ahead(hours=30))
    add("in 4 days", DOWNTOWN, starts_at=ahead(days=4))
    r = bare.post(
        "/swipe",
        data={"opening_id": first, "liked": "0", "today": "1"},
        headers={"HX-Request": "true"},
    )
    assert "in 30 hours" in r.text and "in 4 days" not in r.text
    r = bare.post(
        "/swipe", data={"opening_id": first, "liked": "0", "today": "1"}
    )  # no htmx: back to the deck with the switch still on
    assert r.url.path == "/deck" and r.url.query == b"today=1"


def test_when_free_today_with_nothing_soon_says_so(bare):
    save(bare, when_form())
    add("in 4 days", DOWNTOWN, starts_at=ahead(days=4))
    page = bare.get("/deck?today=1").text
    assert "Nothing in the next 48 hours" in page
    assert 'href="/deck"' in page and "Show everything" in page  # one tap, switch off


def test_when_commitment_one_time_ranks_a_one_off_above_a_weekly(bare):
    save(bare, when_form(availability=["sat_morning"], commitment="one_time"))
    sat = next_weekday(5, "09:00")
    add("weekly", DOWNTOWN, starts_at=sat, recurrence="weekly")
    add("one-off", DOWNTOWN, starts_at=sat, recurrence="one_time")
    assert list(cards(bare)) == ["one-off", "weekly"]
    assert match.commitment_fits("recurring", "weekly")
    assert not match.commitment_fits("recurring", "one_time")
    assert match.commitment_fits("one_time", "flexible")
    assert match.commitment_fits("either", "weekly")


def test_when_a_full_week_ranks_after_next_week_with_a_chip(bare):
    """A limit of 1 and one shift booked that week (Monday to Sunday, Los Angeles)."""
    today = datetime.now(LA).date()
    monday = today + timedelta(days=7 - today.weekday())

    def thursday(weeks):
        return f"{monday + timedelta(days=3 + 7 * weeks)}T10:00"

    booked = add("booked", DOWNTOWN, starts_at=thursday(0), status="filled")
    mid = matches.insert(opening_id=booked, volunteer_id=uid_of(bare),
                         status="accepted", created_at=data._now()).id  # fmt: skip
    shifts.insert(match_id=mid, starts_at=f"{monday + timedelta(days=1)}T09:00",
                  status="scheduled", created_at=data._now())  # fmt: skip
    add("that week", DOWNTOWN, starts_at=thursday(0))
    add("next week", DOWNTOWN, starts_at=thursday(1))
    save(bare, when_form(availability=["thu_morning"]))
    assert list(cards(bare)) == ["that week", "next week"]  # no limit: soonest first
    r = save(bare, when_form(availability=["thu_morning"], weekly_limit="1"))
    assert r.status_code == 303
    assert data.preferences(uid_of(bare))["weekly_limit"] == 1
    ranked = cards(bare)
    assert list(ranked) == ["next week", "that week"]
    assert ranked["that week"]["why"][-1]["text"] == "Your week is full"
    assert "Your week is full" not in [w["text"] for w in ranked["next week"]["why"]]


def test_when_free_dates_are_future_only_and_at_most_ten(bare):
    yesterday = ahead(days=-1)[:10]
    r = save(bare, when_form(free_date_new=yesterday))
    assert r.status_code == 200 and "from today on" in r.text
    r = save(bare, when_form(free_date_new=ahead(days=400)[:10]))
    assert r.status_code == 200 and "within the next year" in r.text
    for odd in ("20991231", "2099-1-5", "9999-12-31"):
        r = save(bare, when_form(free_date_new=odd))
        assert r.status_code == 200 and ("wasn't" in r.text or "next year" in r.text), (
            odd
        )
    eleven = [ahead(days=i + 1)[:10] for i in range(11)]
    r = save(bare, when_form(free_dates=eleven))
    assert "Up to 10 dates" in r.text
    r = save(bare, when_form(free_dates=["next tuesday"]))
    assert "wasn't recognized" in r.text
    r = save(bare, when_form(free_dates=eleven[:10]))
    assert r.status_code == 303
    page = bare.get("/me/preferences/when").text
    assert (
        page.count('name="free_dates"') == 10
    )  # all kept, and no field to add an 11th
    assert 'id="free_date_new"' not in page


def test_when_past_free_dates_are_pruned_when_read(bare):
    soon = ahead(days=2)[:10]
    profiles.update(id=uid_of(bare), free_dates=json.dumps(["2020-01-04", soon]))
    assert data.preferences(uid_of(bare))["free_dates"] == [soon]


@pytest.mark.parametrize(
    "over",
    [
        {"availability": ["weekday_morning"]},  # the older buckets: the grid only
        {"availability": ["mon_brunch"]},
        {"horizon_days": "9"},
        {"commitment": "sometimes"},
    ],
)
def test_when_an_unknown_answer_saves_nothing(bare, over):
    r = save(bare, when_form(**over))
    assert r.status_code == 200 and "wasn't recognized" in r.text
    assert places_of(bare) == AT_START[bare.home]  # nothing saved


def test_when_the_grid_shows_21_slots_and_a_legacy_bucket_as_days(bare):
    profiles.update(id=uid_of(bare), availability='["weekday_morning"]')
    page = bare.get("/me/preferences/when").text
    assert page.count('name="availability"') == 21
    for d in ("mon", "tue", "wed", "thu", "fri"):
        assert f'value="{d}_morning" checked' in page
    assert 'value="sat_morning" checked' not in page


def test_when_migrated_weekday_morning_becomes_mon_to_fri_mornings(bare):
    uid = uid_of(bare)
    profiles.update(
        id=uid, availability='["weekday_morning", "weekend_evening", "tue_evening"]'
    )
    with db.conn:
        day_slots_migration.up(db.conn)
        day_slots_migration.up(db.conn)  # a second run changes nothing
    assert json.loads(profiles[uid].availability) == [
        "mon_morning",
        "tue_morning",
        "tue_evening",
        "wed_morning",
        "thu_morning",
        "fri_morning",
        "sat_evening",
        "sun_evening",
    ]


def test_when_the_migration_and_match_agree():
    for values in (["weekday_morning"], ["weekend_afternoon", "mon_evening"], []):
        assert day_slots_migration.slots(values) == match.day_slots(values)


def test_when_a_slot_is_day_specific():
    assert match.availability_slot("2026-10-20T18:00") == "tue_evening"
    assert match.availability_slot("2026-10-24T09:00") == "sat_morning"
    assert match.availability_overlap(["tue_evening"], "2026-10-20T18:00") == 1
    assert match.availability_overlap(["tue_evening"], "2026-10-21T18:00") == 0
    assert (
        match.availability_overlap([], "2026-10-21T18:00", free_dates=["2026-10-21"])
        == 1
    )


def test_when_the_nonprofit_sees_the_weekday_weekend_summary(bare, coord):
    save(bare, when_form(availability=["tue_evening", "sat_morning"]))
    opening = add("near", DOWNTOWN, starts_at=ahead(days=3))
    data.swipe(uid_of(bare), opening, True)
    inbox = coord.get("/org/inbox").text
    assert "Weekday evenings" in inbox and "Weekend mornings" in inbox
    assert "tue_evening" not in inbox
    assert match.general_buckets(["tue_evening", "sat_morning", "sun_morning"]) == [
        "weekday_evening",
        "weekend_morning",
    ]


def test_when_free_dates_go_with_the_account(bare):
    save(bare, when_form(free_dates=[ahead(days=2)[:10]]))
    uid = uid_of(bare)
    data.delete_account(uid)
    assert profiles[uid].free_dates is None


def test_verified_hours_preference_goes_with_the_account(bare):
    save(bare, work_form(verified_hours="only"))
    uid = uid_of(bare)
    assert profiles[uid].verified_hours
    data.delete_account(uid)
    assert profiles[uid].verified_hours is None


# --- kind of work --------------------------------------------------------------------------


def work_form(**over):
    return when_form(orientation_ok="1", **over)


def test_work_an_opening_that_says_nothing_scores_half():
    for mine in (
        {
            "setting": "outdoor",
            "effort": "active",
            "group_size": "big",
            "first_timer": True,
        },
        {
            "setting": "either",
            "effort": "any",
            "group_size": "any",
            "first_timer": False,
        },
    ):
        assert match.work_fit(mine, {}) == 0.5
        assert match.work_fit(mine, {"setting": "", "effort": None}) == 0.5
    assert match.language_fit(["es"], []) == 0.5
    assert match.language_fit([], ["es"]) == 0.5
    assert match.language_fit(["es", "ko"], ["ko"]) == 1


def test_work_an_outdoor_active_opening_ranks_first_for_an_outdoor_active_volunteer(
    bare,
):
    r = save(bare, work_form(setting="outdoor", effort="active"))
    assert r.status_code == 303, r.text
    add("indoor and light", DOWNTOWN, setting="indoor", effort="light")
    add("says nothing", DOWNTOWN)
    add("outdoor and active", DOWNTOWN, setting="outdoor", effort="active")
    assert list(cards(bare)) == [
        "outdoor and active",
        "says nothing",
        "indoor and light",
    ]


def test_work_a_volunteer_who_does_not_mind_ranks_them_alike(bare):
    save(bare, work_form())
    add("says nothing", DOWNTOWN, starts_at="2099-01-03T09:00")
    add("outdoor and active", DOWNTOWN, setting="outdoor", effort="active",
        starts_at="2099-01-03T10:00")  # fmt: skip
    assert list(cards(bare)) == ["says nothing", "outdoor and active"]  # by time


def test_work_first_timer_friendly_lifts_an_opening_for_a_newcomer(bare):
    save(bare, work_form(first_timer="1"))
    add("no word", DOWNTOWN)
    add("first-timer friendly", DOWNTOWN, first_timer_ok=True)
    assert next(iter(cards(bare))) == "first-timer friendly"


def test_work_a_spoken_language_that_helps_lifts_an_opening(bare):
    save(bare, work_form(languages=["es"]))
    add("korean helps", DOWNTOWN, languages='["ko"]')
    add("spanish helps", DOWNTOWN, languages='["es"]')
    add("no languages", DOWNTOWN)
    assert next(iter(cards(bare))) == "spanish helps"


def test_work_orientation_not_ok_hides_a_separate_orientation(bare):
    save(bare, when_form())  # orientation_ok unticked
    add("same day", DOWNTOWN, orientation_required="same_day")
    hidden = add("separate session", DOWNTOWN, orientation_required="separate")
    assert list(cards(bare)) == ["same day"]
    with pytest.raises(LookupError):
        data.swipe(uid_of(bare), hidden, True)
    save(bare, work_form())
    assert set(cards(bare)) == {"same day", "separate session"}


def test_work_a_profile_from_before_shows_separate_orientations():
    assert data._prefs.orientation_ok(type("P", (), {"orientation_ok": None})())


@pytest.mark.parametrize(
    "over",
    [
        {"setting": "underwater"},
        {"effort": "extreme"},
        {"group_size": "crowd"},
        {"languages": ["klingon"]},
    ],
)
def test_work_an_unknown_answer_saves_nothing(bare, over):
    r = save(bare, work_form(**over))
    assert r.status_code == 200 and "wasn't recognized" in r.text


def test_work_answers_are_stored_and_shown(bare):
    save(
        bare,
        work_form(
            setting="indoor", effort="light", group_size="solo", languages=["tl"]
        ),
    )
    p = profiles[uid_of(bare)]
    assert (p.setting, p.effort, p.group_size, p.languages) == (
        "indoor",
        "light",
        "solo",
        '["tl"]',
    )
    assert p.orientation_ok and not p.first_timer
    page = bare.get("/me/preferences/work").text
    assert 'name="setting" value="indoor" checked' in page
    assert 'name="languages" value="tl" checked' in page
    assert 'name="orientation_ok" value="1" checked' in page


# --- dealbreakers, surprise, why -------------------------------------------------------------

FOOD, ANIMALS, PARKS = "food-insecurity", "animal-shelters", "environment-parks"


def prefer_all(**over):
    """The form with every dealbreaker set to Prefer (distance included)."""
    return {**work_form(), "only_distance": "prefer", **over}


def test_dealbreaker_distance_prefer_shows_what_is_out_of_reach_lower(bare):
    save(bare, prefer_all(**place_fields(1, "Home", "Downtown", minutes="15")))
    add("pasadena", PASADENA, starts_at="2099-01-03T08:00")
    add("downtown", DOWNTOWN, starts_at="2099-01-03T09:00")
    assert list(cards(bare)) == ["downtown", "pasadena"]
    save(bare, work_form(**place_fields(1, "Home", "Downtown", minutes="15")))
    assert list(cards(bare)) == ["downtown"]


def test_dealbreaker_distance_is_on_for_a_profile_from_before(bare):
    assert data.preferences(uid_of(bare))["dealbreakers"] == ["distance"]
    page = bare.get("/me/preferences/causes").text
    assert 'name="only_distance" value="only" checked' in page
    assert 'name="only_causes" value="prefer" checked' in page


def test_dealbreaker_causes_only_shows_loved_and_open(bare):
    form = {"cause_food-insecurity": "love", "cause_environment-parks": "open"}
    save(bare, work_form(**form))
    add("animals", DOWNTOWN, causes=json.dumps([ANIMALS]))
    add("parks", DOWNTOWN, causes=json.dumps([PARKS]))
    add("food", DOWNTOWN)
    assert list(cards(bare)) == ["food", "parks", "animals"]  # prefer: unrated last
    save(bare, work_form(only_causes="only", **form))
    assert list(cards(bare)) == ["food", "parks"]


def test_dealbreaker_when_only_shows_free_times_and_flexible(bare):
    save(bare, work_form(only_when="only", availability=["sat_morning"]))
    add("saturday morning", DOWNTOWN, starts_at=next_weekday(5, "09:00"))
    add("monday evening", DOWNTOWN, starts_at=next_weekday(0, "19:00"))
    add("flexible monday", DOWNTOWN, starts_at=next_weekday(0, "19:00"),
        recurrence="flexible")  # fmt: skip
    assert set(cards(bare)) == {"saturday morning", "flexible monday"}


def test_dealbreaker_when_is_ignored_by_free_today(bare):
    save(bare, work_form(only_when="only", availability=[]))
    add("in 3 hours", DOWNTOWN, starts_at=ahead(hours=3))
    assert cards(bare) == {}
    assert [c["title"] for c in data.deck(uid_of(bare), 5, today=True)] == [
        "in 3 hours"
    ]


def test_dealbreaker_work_hides_a_contradiction_but_not_silence(bare):
    save(bare, work_form(only_work="only", setting="outdoor"))
    add("indoors", DOWNTOWN, setting="indoor")
    add("outdoors", DOWNTOWN, setting="outdoor")
    add("says nothing", DOWNTOWN)
    assert set(cards(bare)) == {"outdoors", "says nothing"}
    assert match.work_allowed({"setting": "either"}, {"setting": "indoor"})


def test_dealbreaker_hidden_cannot_be_liked_by_id(bare):
    save(bare, work_form(only_work="only", setting="outdoor"))
    oid = add("indoors", DOWNTOWN, setting="indoor")
    with pytest.raises(LookupError):
        data.swipe(uid_of(bare), oid, True)


def food_world(n=9):
    """n loved-cause openings, then one animal-shelter one that ranks below them all."""
    for i in range(n):
        add(f"food {i}", DOWNTOWN, starts_at=f"2099-01-{i + 1:02d}T09:00")
    add("animals", DOWNTOWN, causes=json.dumps([ANIMALS]),
        starts_at="2099-01-01T08:00")  # fmt: skip


def test_surprise_puts_a_card_from_outside_love_every_eighth(bare):
    save(bare, work_form(surprise="1"))
    food_world()
    got = data.deck(uid_of(bare), 10)
    assert [c["title"] for c in got].index("animals") == 7
    assert [c["surprise"] for c in got] == [i == 7 for i in range(10)]
    assert {"text": "Surprise pick", "slug": None} in got[7]["why"]


def test_surprise_counts_the_cards_already_swiped(bare):
    save(bare, work_form(surprise="1"))
    food_world(n=12)
    uid = uid_of(bare)
    for c in data.deck(uid, 7):
        assert c["title"] != "animals"
        data.swipe(uid, c["id"], False)
    (top,) = data.deck(uid, 1)  # the 8th card met
    assert top["title"] == "animals" and top["surprise"]
    assert "Surprise pick" in bare.get("/deck").text


def test_surprise_off_leaves_the_ranking_alone(bare):
    save(bare, work_form())
    food_world()
    assert [c["title"] for c in data.deck(uid_of(bare), 10)][-1] == "animals"
    assert not any(c["surprise"] for c in data.deck(uid_of(bare), 10))


def test_why_chips_say_cause_travel_time_and_first_timer(bare):
    save(bare, work_form(availability=["sat_morning"]))
    add("pantry", DOWNTOWN, starts_at=next_weekday(5, "09:00"), first_timer_ok=True)
    (c,) = data.deck(uid_of(bare), 5)
    assert [w["text"] for w in c["why"]] == [
        "♥ Food insecurity",
        "You're free Sat mornings",
        "First-timer friendly",
    ]
    assert c["why"][0]["slug"] == FOOD


def test_why_chips_name_a_free_date_a_work_match_and_a_language(bare):
    day = next_weekday(3, "10:00")
    save(
        bare,
        work_form(free_dates=[day[:10]], setting="outdoor", languages=["es"],
                  **{"cause_food-insecurity": "open"}),
    )  # fmt: skip
    add("trail", DOWNTOWN, starts_at=day, setting="outdoor", languages='["es"]')
    (c,) = data.deck(uid_of(bare), 5)
    texts = [w["text"] for w in c["why"]]
    assert texts[0] == "Food insecurity"  # open, so no heart
    assert texts[1:] == ["You're free that day", "Outdoors", "Spanish helps"]
    profiles.update(id=uid_of(bare), free_dates="[]")
    (c,) = data.deck(uid_of(bare), 5)
    assert [w["text"] for w in c["why"]][1:] == ["Outdoors", "Spanish helps"]


def test_why_every_card_has_one_to_four_chips(user):
    """The seeded world, as onboarding leaves a volunteer: every card says why."""
    got = data.deck(uid_of(user), 50)
    assert got
    for c in got:
        assert 1 <= len(c["why"]) <= 4, c
        assert c["why"][0]["slug"] in c["slugs"]
    assert got[0]["why"][0]["text"] in user.get("/deck").text


@pytest.mark.parametrize("over", [{"only_causes": "maybe"}, {"only_when": "yes"}])
def test_dealbreaker_an_unknown_answer_saves_nothing(bare, over):
    r = save(bare, work_form(**over))
    assert r.status_code == 200 and "wasn't recognized" in r.text


def test_dealbreaker_and_surprise_answers_are_stored(bare):
    save(bare, work_form(only_causes="only", only_work="only", surprise="1"))
    p = profiles[uid_of(bare)]
    assert json.loads(p.dealbreakers) == ["causes", "distance", "work"]
    assert p.surprise
    assert (
        'name="surprise" value="1" checked' in bare.get("/me/preferences/causes").text
    )
    assert (
        'name="only_work" value="only" checked' in bare.get("/me/preferences/work").text
    )


# --- the deck's filter sheet and the empty-deck suggestion -----------------------------------

KOREATOWN = geo.neighborhood_latlon("Koreatown")[1]  # about 5 km from Downtown


def downtown_15(client, **over):
    """Home Downtown, 15 min by car (8 km): Koreatown in reach, Pasadena not."""
    return save(client, work_form(**place_fields(1, "Home", "Downtown"), **over))


def test_filter_sheet_shows_the_count_and_the_current_answers(bare):
    downtown_15(bare)
    add("downtown", DOWNTOWN)
    add("pasadena", PASADENA)
    page = bare.get("/deck").text
    assert ">1 opening<" in page
    assert 'name="max_minutes" value="15" checked' in page
    assert 'name="causes" value="food-insecurity" checked' in page
    assert "Travel from Home" in page


def test_filter_sheet_narrows_this_visit_and_saves_nothing(bare):
    downtown_15(bare)
    add("downtown soon", DOWNTOWN, starts_at=ahead(days=3, at="10:00"))
    pasadena = add("pasadena", PASADENA)  # 17 km: past 15 min by car
    add("animals", DOWNTOWN, causes=json.dumps([ANIMALS]))
    assert ">2 openings<" in bare.get("/deck").text  # downtown soon, animals
    r = bare.post(
        "/deck/filters",
        data={"max_minutes": "45", "causes": [FOOD]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    page = bare.get("/deck").text
    assert ">2 openings<" in page and "Filtered" in page  # downtown soon, pasadena
    bare.post("/deck/filters", data={"max_minutes": "45", "causes": [FOOD],
                                     "this_week": "1"})  # fmt: skip
    assert ">1 opening<" in bare.get("/deck").text
    # nothing saved: ratings, the place and the horizon are as they were
    p = profiles[uid_of(bare)]
    assert json.loads(p.cause_prefs) == {FOOD: "love"}
    assert json.loads(p.causes) == [FOOD]
    assert places(where="user_id = ?", where_args=[uid_of(bare)])[0].max_minutes == 15
    assert not p.horizon_days
    assert set(cards(bare)) == {"downtown soon", "animals"}
    # a card the view shows can be liked, though the saved deck hides it
    bare.post("/deck/filters", data={"max_minutes": "45", "causes": [FOOD]})
    r = bare.post("/swipe", data={"opening_id": pasadena, "liked": "1"},
                  headers={"HX-Request": "true"})  # fmt: skip
    assert "isn't available" not in r.text
    assert data.my_matches(uid_of(bare))[0]["title"] == "pasadena"
    r = bare.post("/deck/filters", data={"clear": "1"}, follow_redirects=False)
    page = bare.get("/deck").text
    assert ">2 openings<" in page and "Filtered" not in page


def test_filter_sheet_offers_only_causes_that_are_not_ruled_out(bare):
    downtown_15(bare, **{"cause_animal-shelters": "no"})
    page = bare.get("/deck").text
    assert 'name="causes" value="food-insecurity" checked' in page
    assert 'name="causes" value="animal-shelters"' not in page


def test_filter_sheet_minutes_show_only_while_distance_limits(bare):
    downtown_15(bare)
    assert 'name="max_minutes"' in bare.get("/deck").text
    save(bare, prefer_all(**place_fields(1, "Home", "Downtown")))
    assert 'name="max_minutes"' not in bare.get("/deck").text


def test_filter_sheet_is_reset_by_saving_preferences(bare):
    downtown_15(bare)
    bare.post("/deck/filters", data={"max_minutes": "60", "causes": [FOOD]})
    assert "Filtered" in bare.get("/deck").text
    downtown_15(bare)
    assert "Filtered" not in bare.get("/deck").text


@pytest.mark.parametrize(
    "bad",
    [{"max_minutes": "20"}, {"max_minutes": "x"}, {"causes": ["nonsense"]}],
)
def test_filter_sheet_unknown_values_are_refused(bare, bad):
    downtown_15(bare)
    r = bare.post("/deck/filters", data={"max_minutes": "15", **bad})
    assert r.status_code == 400


def test_filter_sheet_on_a_sign_up_profile_creates_no_place(bare):
    from_before_places(bare)
    profiles.update(id=uid_of(bare), radius_km=16)
    bare.post("/deck/filters", data={"max_minutes": "60", "causes": [FOOD]})
    assert places() == []
    assert "Filtered" in bare.get("/deck").text


def test_empty_deck_with_a_filter_offers_to_clear_it(bare):
    downtown_15(bare)
    add("animals", DOWNTOWN, causes=json.dumps([ANIMALS]))
    bare.post("/deck/filters", data={"max_minutes": "15", "causes": [FOOD]})
    page = bare.get("/deck").text
    assert "Clear filters: 1 more" in page
    bare.post("/deck/relax", data={"key": "clear_view"})
    assert "Filtered" not in bare.get("/deck").text


def test_empty_deck_names_the_change_that_adds_most(bare):
    downtown_15(bare)
    add("pasadena", PASADENA)
    add("pasadena too", PASADENA)
    assert cards(bare) == {}
    # 30 min (16 km) adds nothing; 45 min (24 km) is the shortest that does
    assert data.relaxations(uid_of(bare)) == {
        "key": "widen:0:45",
        "label": "Widen to 45 min",
        "more": 2,
    }
    assert "Widen to 45 min: 2 more" in bare.get("/deck").text
    r = bare.post("/deck/relax", data={"key": "widen:0:45"}, follow_redirects=False)
    assert r.headers["location"] == "/deck"
    assert set(cards(bare)) == {"pasadena", "pasadena too"}


def test_empty_deck_can_suggest_looking_further_ahead_or_a_dealbreaker(bare):
    downtown_15(bare, horizon_days="7")
    add("in 10 days", DOWNTOWN, starts_at=ahead(days=10, at="10:00"))
    assert data.relaxations(uid_of(bare))["label"] == "Look further ahead"
    bare.post("/deck/relax", data={"key": "horizon"})
    assert set(cards(bare)) == {"in 10 days"}
    downtown_15(bare, only_work="only", setting="outdoor")
    add("indoors", DOWNTOWN, setting="indoor")
    profiles.update(id=uid_of(bare), horizon_days=0)
    swipe_all = [c["id"] for c in data.deck(uid_of(bare), 10)]
    for oid in swipe_all:
        data.swipe(uid_of(bare), oid, False)
    assert data.relaxations(uid_of(bare))["label"] == "Show any kind of work"
    bare.post("/deck/relax", data={"key": "only_work"})
    assert set(cards(bare)) == {"indoors"}
    assert "work" not in json.loads(profiles[uid_of(bare)].dealbreakers)


def test_empty_deck_with_nothing_to_relax_suggests_nothing(bare):
    save(bare, work_form(**place_fields(1, "Home", "Downtown", minutes="0")))
    assert data.relaxations(uid_of(bare)) is None
    page = bare.get("/deck").text
    assert "more</button>" not in page and "You're all caught up" in page


@pytest.mark.parametrize("key", ["only_when", "widen:9:45", "widen:0:x", "widen:0:20"])
def test_empty_deck_relax_with_a_stale_key_changes_nothing(bare, key):
    downtown_15(bare)
    r = bare.post("/deck/relax", data={"key": key}, follow_redirects=False)
    assert r.status_code == 303
    assert json.loads(profiles[uid_of(bare)].dealbreakers) == ["distance"]
    assert places(where="user_id = ?", where_args=[uid_of(bare)])[0].max_minutes == 15
    with pytest.raises(ValueError):
        data.apply_relaxation(uid_of(bare), key)


def test_empty_deck_views_are_counted_once_an_hour(bare):
    downtown_15(bare)
    bare.get("/deck")
    bare.get("/deck")  # a refresh within the hour
    assert len(deck_empties()) == 1
    (row,) = deck_empties()
    deck_empties.update(id=row.id, at="2020-01-01T00:00:00+00:00")
    bare.get("/deck?today=1")
    assert [(e.volunteer_id, bool(e.today)) for e in deck_empties()] == [
        (uid_of(bare), False),
        (uid_of(bare), True),
    ]
    m = pilot_metrics(data.metrics_rows())
    assert m["empty_deck"] == {"views": 2, "volunteers": 1}


# --- free dates, free today, dealbreakers and migrations ----------------------------------


def test_when_a_kept_date_that_passed_drops_silently_and_a_new_one_is_added(bare):
    """A chip for a date that passed while the page was open doesn't block the save."""
    soon = ahead(days=3)[:10]
    r = save(bare, when_form(free_dates=[ahead(days=-1)[:10]], free_date_new=soon))
    assert r.status_code == 303
    assert json.loads(profiles[uid_of(bare)].free_dates) == [soon]


def test_when_free_today_orders_by_start_not_fit(bare):
    """Within the 48 hours the soonest shift leads even when it fits far worse."""
    save(bare, when_form(availability=[], **{"cause_animal-shelters": "open"}))
    later = ahead(hours=30)
    add("sooner, poor fit", KOREATOWN, starts_at=ahead(hours=4),
        causes=json.dumps([ANIMALS]))  # fmt: skip
    add("later, best fit", DOWNTOWN, starts_at=later, first_timer_ok=True)
    assert list(cards(bare)) == ["later, best fit", "sooner, poor fit"]  # by fit
    today = [c["title"] for c in data.deck(uid_of(bare), 5, today=True)]
    assert today == ["sooner, poor fit", "later, best fit"]


def test_when_free_today_breaks_a_tie_by_distance_then_remote(bare):
    save(bare, when_form())
    t = ahead(hours=6)
    add("remote", DOWNTOWN, starts_at=t, remote=True)
    add("farther", KOREATOWN, starts_at=t)
    add("nearer", DOWNTOWN, starts_at=t)
    today = [c["title"] for c in data.deck(uid_of(bare), 5, today=True)]
    assert today == ["nearer", "farther", "remote"]


def test_place_an_error_page_echoes_only_a_rounded_point(bare):
    r = save(
        bare,
        place_fields(1, "Home", "My location", lat="34.056789", lon="-118.234561"),
        **{"cause_food-insecurity": "no"},  # an error after the place was read
    )
    assert r.status_code == 200 and "at least one cause" in r.text
    assert "056789" not in r.text and "234561" not in r.text
    assert 'value="34.06"' in r.text and 'value="-118.23"' in r.text
    r = save(bare, place_fields(1, "Home", "My location", lat="40.712345", lon="-74.0"))
    assert "isn't in Los Angeles County" in r.text and "712345" not in r.text


def test_dealbreaker_an_inactive_not_for_me_does_not_hide_an_active_cause(
    homelessness_off, bare
):
    (dead,) = [c for c in causes() if not c.active][:1]
    save(bare, when_form())
    profiles.update(
        id=uid_of(bare),
        cause_prefs=json.dumps({FOOD: "love", dead.slug: "no"}),
    )
    add("food and the paused cause", DOWNTOWN, causes=json.dumps([FOOD, dead.slug]))
    assert list(cards(bare)) == ["food and the paused cause"]


def test_migrations_skip_a_malformed_row(tmp_path):
    import apsw

    conn = apsw.Connection(str(tmp_path / "odd.db"))
    conn.execute(
        "CREATE TABLE profile (id INTEGER PRIMARY KEY, causes TEXT, availability TEXT)"
    )
    conn.execute(
        "INSERT INTO profile VALUES (1, 'not json', 'null'), (2, 'null', '{]'), "
        "(3, '[\"food-insecurity\"]', '[\"weekday_morning\"]')"
    )
    cause_prefs_migration.up(conn)
    day_slots_migration.up(conn)
    rows = conn.execute(
        "SELECT id, cause_prefs, availability FROM profile ORDER BY id"
    ).fetchall()
    assert rows[0] == (1, None, "null") and rows[1] == (2, None, "{]")
    assert json.loads(rows[2][1]) == {"food-insecurity": "love"}
    assert json.loads(rows[2][2])[0] == "mon_morning"


# --- swipes, stale forms, languages and why chips -----------------------------------------


def test_free_today_cards_can_be_swiped_under_the_when_dealbreaker(bare):
    """The 48-hour deck ignores the volunteer's times; liking its card must count."""
    t = ahead(hours=3)
    elsewhere = (
        "mon_morning" if match.availability_slot(t) != "mon_morning" else "tue_evening"
    )
    save(bare, work_form(only_when="only", availability=[elsewhere]))
    oid = add("in 3 hours", DOWNTOWN, starts_at=t)
    add("in 5 hours", DOWNTOWN, starts_at=ahead(hours=5))
    assert oid not in {c["id"] for c in data.deck(uid_of(bare), 5)}  # hidden normally
    r = bare.post("/swipe", data={"opening_id": oid, "liked": "1", "today": "1"},
                  headers={"HX-Request": "true"})  # fmt: skip
    assert data.my_matches(uid_of(bare))[0]["title"] == "in 3 hours"
    assert "in 5 hours" in r.text and "isn't available" not in r.text
    with pytest.raises(LookupError):  # outside today mode it stays hidden
        data.swipe(uid_of(bare), add("in 4 hours", DOWNTOWN,
                                     starts_at=ahead(hours=4)), True)  # fmt: skip


def test_a_swipe_that_does_not_count_says_so(bare):
    save(bare, work_form())
    gone = add("gone", DOWNTOWN, status="closed")
    add("next", DOWNTOWN)
    r = bare.post("/swipe", data={"opening_id": gone, "liked": "1"},
                  headers={"HX-Request": "true"})  # fmt: skip
    assert "isn't available any more" in r.text and "next" in r.text


def test_a_stale_form_keeps_what_it_does_not_carry(bare):
    """A page loaded before the horizon, kind-of-work and only-show fields existed must
    not reset them when saved."""
    save(bare, work_form(only_causes="only", horizon_days="14", commitment="recurring",
                         setting="outdoor", languages=["es"], surprise="1"))  # fmt: skip
    old = {k: v for k, v in BASE.items() if k not in ("only_distance", "sections")}
    r = bare.post("/me/preferences", data={**old, **place_fields(1, "Home", "Downtown")},
                  follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    p = profiles[uid_of(bare)]
    assert json.loads(p.dealbreakers) == ["causes", "distance"]
    assert (p.horizon_days, p.commitment, p.setting) == (14, "recurring", "outdoor")
    assert json.loads(p.languages) == ["es"] and p.surprise and p.orientation_ok


def test_another_language_is_never_a_match(bare):
    assert match.language_fit(["other"], ["other"]) == 0.5
    assert match.languages_shared(["other", "es"], ["other", "es"]) == {"es"}
    save(bare, work_form(languages=["other"]))
    add("needs another language", DOWNTOWN, languages='["other"]')
    (c,) = data.deck(uid_of(bare), 5)
    assert not any("helps" in w["text"] for w in c["why"])


def test_languages_are_stored_once(bare):
    save(bare, work_form(languages=["es", "es", "ko", "es"]))
    assert json.loads(profiles[uid_of(bare)].languages) == ["es", "ko"]


def test_why_chips_show_a_loved_cause_first(bare):
    save(bare, work_form(**{"cause_food-insecurity": "open",
                            "cause_animal-shelters": "love"}))  # fmt: skip
    add("three causes", DOWNTOWN, causes=json.dumps([PARKS, FOOD, ANIMALS]))
    (c,) = data.deck(uid_of(bare), 5)
    assert [w["text"] for w in c["why"][:2]] == ["♥ Animal shelters", "Food insecurity"]


def test_why_first_timer_chip_comes_after_matches_unless_wanted(bare):
    save(bare, work_form(setting="outdoor", languages=["es"], availability=[]))
    add("trail", DOWNTOWN, first_timer_ok=True, setting="outdoor", languages='["es"]')
    (c,) = data.deck(uid_of(bare), 5)
    assert [w["text"] for w in c["why"]][1:] == [
        "Outdoors",
        "Spanish helps",
        "First-timer friendly",
    ]
    save(bare, work_form(setting="outdoor", languages=["es"], first_timer="1",
                         availability=[]))  # fmt: skip
    (c,) = data.deck(uid_of(bare), 5)
    assert [w["text"] for w in c["why"]][1:] == [
        "First-timer friendly",
        "Outdoors",
        "Spanish helps",
    ]


def test_place_names_with_hidden_characters_are_refused(bare):
    r = save(bare, place_fields(1, "Ho\u200dme", "Downtown"))
    assert r.status_code == 200 and "hidden or special characters" in r.text
    r = save(bare, place_fields(1, "Home", "Down\u00adtown"))  # soft hyphen (Cf)
    assert "hidden or special characters" in r.text
    assert places_of(bare) == AT_START[bare.home]  # nothing saved


def test_hidden_chars_categories():
    from app.forms import hidden_chars

    assert not hidden_chars("Sam O'Neil-Smith, Café 東京 ✓")
    for bad in ("a\u200bb", "\u2800", "\uffa0", "a\x00", "\ue000", "a\u2029b"):
        assert hidden_chars(bad), repr(bad)
    assert hidden_chars("line\nbreak") and not hidden_chars("line\nbreak", True)
    # a zero-width joiner: only in multi-line text, and only inside an emoji sequence
    flag = "\U0001f3f3\ufe0f\u200d\U0001f308"
    assert hidden_chars(flag) and not hidden_chars(flag, True)
    assert hidden_chars("a\u200db", True)  # never between letters
    assert hidden_chars("\u200dab", True) and hidden_chars("ab\u200d", True)
    # combining marks: up to 3 in a row
    assert not hidden_chars("e\u0301\u0301\u0301")
    assert hidden_chars("e\u0301\u0301\u0301\u0301")
    assert hidden_chars("tab\there") and not hidden_chars("tab\there", True)


# --- filters, views and kept ratings ------------------------------------------------------


def test_a_causes_only_filter_does_not_pin_the_travel_time(bare):
    """The sheet posts its pre-checked minutes too; only what differs is kept."""
    downtown_15(bare)
    pasadena = add("pasadena", PASADENA)  # 17 km: needs 45 min by car
    every = [c.slug for c in data.active_causes()]  # every cause: no filter at all
    bare.post("/deck/filters", data={"max_minutes": "15", "causes": every})
    assert "Filtered" not in bare.get("/deck").text  # nothing differs: no view
    bare.post("/deck/filters", data={"max_minutes": "15", "causes": []})
    page = bare.get("/deck").text
    assert "Filtered" in page and "Clear filters: 1 more" not in page  # 0 cards anyway
    bare.post("/deck/filters", data={"clear": "1"})
    # empty deck, no view: the offer widens the saved place, and the card does appear
    assert "Widen to 45 min: 1 more" in bare.get("/deck").text
    bare.post("/deck/relax", data={"key": "widen:0:45"})
    assert pasadena in {c["id"] for c in data.deck(uid_of(bare), 5)}


def test_under_a_view_an_offer_always_adds_what_it_promises(bare):
    """A view pinning 30 min: widening the saved place would change nothing on screen,
    so it is not offered; clearing the view comes first when it helps."""
    downtown_15(bare)
    add("pasadena", PASADENA)
    add("animals", DOWNTOWN, causes=json.dumps([ANIMALS]))
    uid = uid_of(bare)
    pinned = {"max_minutes": 30, "hidden": [ANIMALS]}
    offer = data.relaxations(uid, view=pinned)
    assert offer == {"key": "clear_view", "label": "Clear filters", "more": 1}
    assert data.relaxations(uid, view={"max_minutes": 30}) is None  # only pasadena left
    before = places(where="user_id = ?", where_args=[uid])[0].max_minutes
    bare.post("/deck/filters", data={"max_minutes": "30", "causes": [FOOD]})
    page = bare.get("/deck").text
    assert "Widen" not in page and "Clear filters: 1 more" in page
    assert places(where="user_id = ?", where_args=[uid])[0].max_minutes == before


def test_empty_deck_views_keep_the_free_today_split(bare):
    downtown_15(bare)
    bare.get("/deck")
    bare.get("/deck?today=1")
    bare.get("/deck")
    bare.get("/deck?today=1")
    assert sorted(bool(e.today) for e in deck_empties()) == [False, True]


def test_a_rating_the_page_did_not_carry_is_kept(bare):
    """A page loaded before a cause came back has no radios for it: its rating stays."""
    save(bare, work_form(**{"cause_animal-shelters": "no"}))
    form = {k: v for k, v in work_form().items() if k != "cause_animal-shelters"}
    save(bare, form)
    assert json.loads(profiles[uid_of(bare)].cause_prefs)[ANIMALS] == "no"


# --- verified hours -----------------------------------------------------------------------


def test_verified_hours_only_hides_nonprofits_that_do_not_confirm(bare):
    orgs.update(id=orgs()[0].id, confirms_hours=True)
    add("does not", DOWNTOWN, org_id=orgs()[1].id)  # first on a tie
    add("confirms", DOWNTOWN)
    save(bare, work_form(verified_hours="only"))
    assert set(cards(bare)) == {"confirms"}
    for mode in ("", "prefer"):
        save(bare, work_form(verified_hours=mode))
        assert set(cards(bare)) == {"confirms", "does not"}
    shown = cards(bare)  # prefer saved last: the confirming one is first, and says so
    assert list(shown) == ["confirms", "does not"]
    assert "Confirms your hours" in str(shown["confirms"]["why"])
    assert "Confirms your hours" not in str(shown["does not"]["why"])


def test_verified_hours_only_cannot_be_liked_by_id_and_offers_a_way_out(bare):
    save(bare, work_form(verified_hours="only"))
    oid = add("does not", DOWNTOWN)
    with pytest.raises(LookupError):
        data.swipe(uid_of(bare), oid, True)
    assert data.relaxations(uid_of(bare))["key"] == "only_verified"
    data.apply_relaxation(uid_of(bare), "only_verified")
    assert [c["title"] for c in data.deck(uid_of(bare), 5)] == ["does not"]
    assert data.preferences(uid_of(bare))["verified_hours"] == "prefer"


def test_verified_hours_a_coordinator_sets_it_and_a_non_member_cannot(coord):
    org = coord.org_id
    coord.post(f"/org/organization/{org}", data={"about": "", "confirms_hours": "1"})
    assert orgs[org].confirms_hours
    coord.post(f"/org/organization/{org}", data={"about": ""})
    assert not orgs[org].confirms_hours
    other = coordinator(1)
    r = other.post(f"/org/organization/{org}", data={"confirms_hours": "1"})
    assert r.status_code == 403 and not orgs[org].confirms_hours


def test_a_volunteer_who_speaks_a_language_has_the_language_skill(bare):
    """The skills picker no longer offers "language"; the languages list says it."""
    add("needs a car", DOWNTOWN, skills='["car"]')
    add("needs a language", DOWNTOWN, skills='["language"]')  # made second: wins no tie
    save(bare, work_form())
    assert list(cards(bare)) == ["needs a car", "needs a language"]  # a tie
    save(bare, work_form(languages=["es"]))
    assert json.loads(profiles[uid_of(bare)].skills) == []
    assert list(cards(bare)) == ["needs a language", "needs a car"]
    assert 'value="language"' not in bare.get("/me/preferences/work").text
