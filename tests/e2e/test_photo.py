import io

import pytest
from conftest import jpeg, png
from PIL import Image

from app.db import profiles
from e2e.helpers import SE, guard, sign_in_page

pytestmark = pytest.mark.e2e


def big_photo():
    """A 3000 x 2000 JPEG of noise (it compresses badly, like a real photo): a few MB."""
    buf = io.BytesIO()
    Image.effect_noise((3000, 2000), 80).convert("RGB").save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_a_photo_is_previewed_then_uploaded_shrunk(phone_context):
    page = phone_context.new_page()
    p = sign_in_page(page, "(310) 555-0170")
    page.goto("/me/profile")
    raw = big_photo()
    sent = []
    page.on(
        "request",
        lambda r: (
            sent.append(len(r.post_data_buffer or b""))
            if r.method == "POST" and r.url.endswith("/me/profile/photo")
            else None
        ),
    )
    page.get_by_label("Add a photo").set_input_files(
        {"name": "big.jpg", "mimeType": "image/jpeg", "buffer": raw}
    )
    preview = page.get_by_role("img", name="Your new photo")
    preview.wait_for(state="visible")
    assert preview.get_attribute("src").startswith("blob:")  # the CSP allows it
    page.get_by_role("button", name="Save photo").click()
    page.wait_for_url("**/me/profile?notice=saved")
    assert (
        sent and sent[0] < len(raw) / 2
    )  # the browser's 1600 px JPEG, not the original
    key = profiles[p.id].photo_key
    assert key and page.locator(f'img[src="/photos/thumb/{key}.jpg"]').is_visible()


def test_a_card_with_photo_logo_and_link_keeps_its_buttons_in_view(browser, live_url):
    """375 x 667, the smallest common phone: the photo band, the badge and the event link
    still leave Pass and Like above the tab bar."""
    from app import data, photos
    from app.db import openings, orgs

    for org in orgs():
        photo = data.store_photo("org_photo", org.id, photos.process(jpeg(), "card"))
        logo = data.store_photo("org_logo", org.id, photos.process(png(), "logo"))
        orgs.update(id=org.id, photo_key=photo, logo_key=logo)
    for o in openings():
        openings.update(id=o.id, event_url="https://www.example.org/volunteer")
    ctx = guard(
        browser.new_context(
            viewport=SE,
            is_mobile=True,
            has_touch=True,
            base_url=live_url,
        )
    )
    page = ctx.new_page()
    sign_in_page(page, "(310) 555-0171")
    page.goto("/deck")
    assert page.locator('#deck-area img[src^="/photos/"]').count() == 2  # photo, badge
    assert page.get_by_role("link", name="See it on example.org").is_visible()
    bar = page.get_by_role("navigation", name="Main").bounding_box()
    for name in ("Pass", "Like"):
        box = page.get_by_role("button", name=name).bounding_box()
        assert box["y"] + box["height"] <= bar["y"], name
    ctx.close()
