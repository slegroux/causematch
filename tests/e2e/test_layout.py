import pytest
from conftest import coordinator_for
from test_swipe import guard, offline_under_csp, sign_in_page  # noqa: F401  (fixture)

from app import data
from app.db import openings, orgs

pytestmark = pytest.mark.e2e

SE = {"width": 375, "height": 667}  # iPhone SE, the smallest phone in common use


@pytest.fixture
def se_page(browser, live_url):
    ctx = guard(
        browser.new_context(
            viewport=SE, is_mobile=True, has_touch=True, base_url=live_url
        )
    )
    yield ctx.new_page()
    ctx.close()


def accepted_match(uid):
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(uid, opening.id, True)
    data.decide(data.coordinator_by_email(coordinator_for(0)[1]), m.id, True)
    return m.id


def test_volunteer_pages_fit_a_small_phone_with_the_tab_bar_in_view(se_page):
    p = sign_in_page(se_page, "(310) 555-0160")
    match_id = accepted_match(p.id)
    for path, tab in (
        ("/deck", "Openings"),
        ("/matches", "Matches"),
        ("/history", "Me"),
        ("/profile", "Me"),
        (f"/chat/{match_id}", "Matches"),
    ):
        se_page.goto(path)
        width = se_page.evaluate(
            "[document.scrollingElement.scrollWidth, window.innerWidth]"
        )
        assert width[0] <= width[1], (path, width)
        bar = se_page.get_by_role("navigation", name="Main")
        assert bar.is_visible(), path
        box = bar.bounding_box()
        assert box["y"] + box["height"] == pytest.approx(SE["height"], abs=1), path
        current = bar.locator("[aria-current=page]")
        assert current.inner_text().strip() == tab, path


def test_swipe_buttons_are_big_round_targets(se_page):
    sign_in_page(se_page, "(310) 555-0161")
    se_page.goto("/deck")
    for name in ("Pass", "Like"):
        box = se_page.get_by_role("button", name=name).bounding_box()
        assert box["width"] >= 56 and box["height"] >= 56, (name, box)
        # above the tab bar, so a thumb reaches them without scrolling
        assert box["y"] + box["height"] <= SE["height"] - 64, (name, box)


def test_the_chat_composer_is_in_view_above_the_tab_bar(se_page):
    p = sign_in_page(se_page, "(310) 555-0162")
    match_id = accepted_match(p.id)
    for i in range(30):  # a history taller than the screen
        data.send_message(p.id, match_id, f"Message number {i}")
    se_page.goto(f"/chat/{match_id}")
    se_page.evaluate("window.scrollTo(0, 0)")
    bar = se_page.get_by_role("navigation", name="Main").bounding_box()
    for field in (
        se_page.get_by_label("Message"),
        se_page.get_by_role("button", name="Send"),
    ):
        box = field.bounding_box()
        assert box["y"] + box["height"] <= bar["y"], (box, bar)
        assert box["height"] >= 44, box


def test_an_os_dark_mode_keeps_native_controls_light(browser, live_url):
    ctx = guard(browser.new_context(color_scheme="dark", base_url=live_url))
    page = ctx.new_page()
    page.goto("/login")
    scheme = page.evaluate("getComputedStyle(document.documentElement).colorScheme")
    assert scheme == "light"
    ctx.close()


def test_the_save_bar_covers_nothing_at_the_bottom_of_preferences(se_page):
    sign_in_page(se_page, "(310) 555-0163")
    se_page.goto("/me/preferences")
    se_page.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
    save = se_page.get_by_role("button", name="Save preferences").bounding_box()
    bar = se_page.get_by_role("navigation", name="Main").bounding_box()
    assert save["y"] + save["height"] <= bar["y"], (save, bar)
    # the last section's last control is above the save button, not under it
    last = se_page.locator("label:has(input[name=skills])").last.bounding_box()
    assert last["y"] + last["height"] <= save["y"], (last, save)
