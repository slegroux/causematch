"""The deck's filter sheet and empty-deck suggestion, on a phone, in a real browser."""

import pytest
from conftest import close_all_openings, make_opening
from playwright.sync_api import expect

from app import data, geo
from e2e.helpers import sign_in_page

pytestmark = pytest.mark.e2e

DOWNTOWN = geo.neighborhood_latlon("Downtown")[1]
KOREATOWN = geo.neighborhood_latlon("Koreatown")[1]  # about 5 km from Downtown
PASADENA = geo.neighborhood_latlon("Pasadena")[1]  # about 17 km


def world(uid, minutes):
    """Home Downtown by car within `minutes`, and three openings at 0, 5 and 17 km."""
    close_all_openings()
    for title, (lat, lon) in (
        ("Downtown pantry", DOWNTOWN),
        ("Koreatown kitchen", KOREATOWN),
        ("Pasadena trail day", PASADENA),
    ):
        make_opening(
            title, causes=["food-insecurity"], lat=lat, lon=lon, neighborhood="Somewhere",
            starts_at="2099-01-03T09:00",
        )  # fmt: skip
    data.save_preferences(
        uid,
        causes={"food-insecurity": "love"},
        availability=[],
        skills=[],
        frequency="",
        places=[
            {
                "label": "Home",
                "lat": DOWNTOWN[0],
                "lon": DOWNTOWN[1],
                "area": "Downtown",
                "mode": "car",
                "max_minutes": minutes,
            }
        ],
    )


def test_changing_distance_in_the_sheet_reloads_the_deck_with_the_new_count(phone):
    p = sign_in_page(phone, "(310) 555-0170")
    world(p.id, 15)  # Downtown and Koreatown are in reach, Pasadena is not
    phone.goto("/deck")
    sheet = phone.locator("summary")
    # a round button: the count as a badge, "2 openings, filters" for a screen reader
    assert "2 openings" in sheet.inner_text()
    sheet.click()
    # the open sheet's button is above the tab bar on a small phone
    show = phone.get_by_role("button", name="Show openings").bounding_box()
    bar = phone.get_by_role("navigation", name="Main").bounding_box()
    assert show["y"] + show["height"] <= bar["y"], (show, bar)
    phone.locator("label:has(input[name=max_minutes][value='45'])").click()
    phone.get_by_role("button", name="Show openings").click()
    phone.wait_for_load_state("networkidle")
    assert "3 openings" in phone.locator("summary").inner_text()
    # the sheet's button sits beside the swipe buttons, which still work
    phone.get_by_role("button", name="Pass").click()
    expect(phone.locator("summary")).to_contain_text("2 openings")


def test_an_empty_deck_offers_the_change_that_adds_most(phone):
    p = sign_in_page(phone, "(310) 555-0171")
    world(p.id, 15)
    for o in data.deck(p.id, 10):
        data.swipe(p.id, o["id"], False)
    phone.goto("/deck")
    offer = phone.get_by_role("button", name="Widen to 45 min: 1 more")
    assert offer.is_visible()
    offer.click()
    phone.wait_for_load_state("networkidle")
    assert phone.get_by_role("heading", name="Pasadena trail day").is_visible()
