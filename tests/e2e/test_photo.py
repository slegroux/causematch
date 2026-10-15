import io

import pytest
from PIL import Image
from test_swipe import guard, offline_under_csp, sign_in_page  # noqa: F401  (fixture)

from app.db import profiles

pytestmark = pytest.mark.e2e


@pytest.fixture
def phone_context(browser, playwright, live_url):
    ctx = guard(
        browser.new_context(**playwright.devices["iPhone 13"], base_url=live_url)
    )
    yield ctx
    ctx.close()


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
