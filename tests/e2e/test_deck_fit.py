"""The deck fits one phone screen: the tallest card in practice (an urgent, auto-accepting
call with a photo, two why-chip rows, a two-line title, an event link) under the preview
banner and the Home Screen toast, and Pass, Like and the filter button never cover its
text."""

from datetime import datetime, timedelta

import pytest
from conftest import close_all_openings, coordinator_for, jpeg, make_opening

from app import data, photos
from app import ui as app_ui
from app.db import orgs, profiles
from e2e.helpers import guard, sign_in_page

pytestmark = pytest.mark.e2e

TITLE = "Sample: Help distribute food at a partner pantry"
LINK = "https://www.lafoodbank.org/volunteer/"


def tall_urgent_card(uid):
    """The card of the live preview's smoke run (smoke2/deck.png), for this volunteer."""
    close_all_openings()
    org = orgs()[0]
    orgs.update(
        id=org.id,
        name="Los Angeles Regional Food Bank",
        photo_key=data.store_photo("org_photo", org.id, photos.process(jpeg(), "card")),
        logo_key=data.store_photo(
            "org_logo", org.id, photos.process(jpeg((400, 400)), "logo")
        ),
    )
    start = datetime.now(data.LA) + timedelta(days=5)
    oid = make_opening(
        TITLE, causes=["food-insecurity"], lat=34.06, lon=-118.24,
        neighborhood="Central-Alameda", starts_at=start.strftime("%Y-%m-%dT10:00"),
        recurrence="weekly", slots=5, org_id=org.id, first_timer_ok=True, event_url=LINK,
    ).id  # fmt: skip
    tomorrow = (datetime.now(data.LA) + timedelta(days=1)).date()
    profiles.update(
        id=uid, lat=34.05, lon=-118.25, radius_km=60,
        cause_prefs='{"food-insecurity": "love"}', availability='["sat_morning"]',
        free_dates=f'["{tomorrow}"]', urgent_on=True,
    )  # fmt: skip
    coord, _ = coordinator_for(0)
    data.post_urgent_call(coord, oid, f"{tomorrow}T09:00", 2, True)
    (card,) = data.deck(uid, 5)
    assert card["urgent"] and card["booked"]
    assert len(card["why"]) == 2  # of 4: an urgent card shows its call's lines instead


# every element of the card that shows text of its own
TEXTS = """card => [...card.querySelectorAll('*')].filter(e =>
    [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) &&
    e.offsetParent !== null && !e.closest('.sr-only')
).map(e => { const r = e.getBoundingClientRect();
    return {text: e.textContent.trim().slice(0, 40), top: r.top, bottom: r.bottom}; })"""


@pytest.mark.parametrize(
    "device", ["iPhone 13", "iPhone SE (3rd gen)"]
)  # 390x664, 375x667
def test_a_tall_urgent_card_fits_above_pass_and_like(
    browser, playwright, live_url, monkeypatch, device
):
    monkeypatch.setenv("CAUSEMATCH_DEMO_BANNER", "1")
    monkeypatch.setattr(app_ui, "is_preview", lambda: True)  # its two-line banner
    ctx = guard(browser.new_context(**playwright.devices[device], base_url=live_url))
    page = ctx.new_page()
    p = sign_in_page(page, "(310) 555-0167")
    tall_urgent_card(p.id)
    page.goto("/deck")
    assert page.locator("#a2hs").is_visible()  # the iPhone toast is part of the case
    assert "don't enter real details" in page.locator("[role=note]").inner_text()
    width = page.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
    assert width[0] == width[1]  # the full-width strip adds no sideways scroll
    row = page.locator(".cm-swipe-buttons").bounding_box()
    card = page.locator("[data-swipe-card]")
    covered = [
        t for t in card.evaluate(TEXTS)
        if t["bottom"] > row["y"] and t["top"] < row["y"] + row["height"]
    ]  # fmt: skip
    assert not covered, (device, row, covered)
    # the end of the card: the event link, whole and tappable above the row
    page.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
    link = page.get_by_role("link", name="See it on lafoodbank.org")
    box = link.bounding_box()
    row = page.locator(".cm-swipe-buttons").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= row["y"], (box, row)
    hit = page.evaluate(
        "([x, y]) => document.elementFromPoint(x, y).closest('a')?.href ?? null",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )
    assert hit == LINK
    ctx.close()
