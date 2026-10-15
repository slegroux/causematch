"""The deck's filter sheet and empty-deck suggestion, on a phone, in a real browser."""

import json

import pytest
from playwright.sync_api import expect
from test_swipe import guard, sign_in_page

from app import data, geo
from app.db import Opening, now, openings, orgs

pytestmark = pytest.mark.e2e

SE = {"width": 375, "height": 667}
DOWNTOWN = geo.neighborhood_latlon("Downtown")[1]
KOREATOWN = geo.neighborhood_latlon("Koreatown")[1]  # about 5 km from Downtown
PASADENA = geo.neighborhood_latlon("Pasadena")[1]  # about 17 km


@pytest.fixture
def phone(browser, live_url):
    ctx = guard(
        browser.new_context(
            viewport=SE, is_mobile=True, has_touch=True, base_url=live_url
        )
    )
    yield ctx.new_page()
    ctx.close()


def world(uid, minutes):
    """Home Downtown by car within `minutes`, and three openings at 0, 5 and 17 km."""
    for o in openings():
        openings.update(id=o.id, status="closed")
    for title, (lat, lon) in (
        ("Downtown pantry", DOWNTOWN),
        ("Koreatown kitchen", KOREATOWN),
        ("Pasadena trail day", PASADENA),
    ):
        openings.insert(
            Opening(
                org_id=orgs()[0].id,
                title=title,
                about="",
                causes=json.dumps(["food-insecurity"]),
                skills="[]",
                lat=lat,
                lon=lon,
                neighborhood="Somewhere",
                remote=False,
                starts_at="2099-01-03T09:00",
                recurrence="one_time",
                slots=3,
                status="open",
                created_at=now(),
            )
        )
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
    assert sheet.inner_text().startswith("2 openings")  # then ", filters" for readers
    sheet.click()
    # the open sheet's button is above the tab bar on a small phone
    show = phone.get_by_role("button", name="Show openings").bounding_box()
    bar = phone.get_by_role("navigation", name="Main").bounding_box()
    assert show["y"] + show["height"] <= bar["y"], (show, bar)
    phone.locator("label:has(input[name=max_minutes][value='45'])").click()
    phone.get_by_role("button", name="Show openings").click()
    phone.wait_for_load_state("networkidle")
    assert phone.locator("summary").inner_text().startswith("3 openings")
    # the sheet stays on the card's corner: the swipe buttons still work under it
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
