"""The "It's a match!" takeover fits the smallest phones: the whole dialog, both buttons
included, is on screen without scrolling, centred over the deck."""

import pytest

from e2e.helpers import accepted_match, guard, sign_in_page

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    "device, number",
    [("iPhone SE (3rd gen)", "(310) 555-0191"), ("iPhone 13", "(310) 555-0192")],
)  # 375x667, 390x664
def test_the_match_takeover_fits_without_scrolling(
    browser, playwright, live_url, device, number
):
    ctx = guard(browser.new_context(**playwright.devices[device], base_url=live_url))
    page = ctx.new_page()
    p = sign_in_page(page, number)
    match_id = accepted_match(p.id)
    page.goto("/deck")
    sheet = page.locator("dialog.cm-moment")
    assert sheet.evaluate("d => d.matches(':modal')")
    page.wait_for_timeout(600)  # the pop-in is done
    box = sheet.bounding_box()
    height = page.evaluate("innerHeight")
    assert box["y"] >= 0 and box["y"] + box["height"] <= height, (box, height)
    assert sheet.evaluate("d => d.scrollHeight <= d.clientHeight")  # nothing to scroll
    hi = page.get_by_role("link", name="Say hi")
    keep = page.get_by_role("button", name="Keep swiping")
    for button in (hi, keep):
        b = button.bounding_box()
        assert b["height"] >= 44 and b["y"] + b["height"] <= box["y"] + box["height"]
    assert hi.get_attribute("href") == f"/chat/{match_id}"
    # centred: about as much screen above it as below
    assert abs(box["y"] - (height - box["y"] - box["height"])) <= 2, (box, height)
    keep.click()
    assert sheet.evaluate("d => !d.open")
    ctx.close()
