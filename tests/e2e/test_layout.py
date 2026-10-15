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


def long_chat(page, phone):
    p = sign_in_page(page, phone)
    match_id = accepted_match(p.id)
    for i in range(30):  # a history taller than the screen
        data.send_message(p.id, match_id, f"Message number {i}")
    page.goto(f"/chat/{match_id}")
    return match_id


def test_the_chat_composer_and_links_are_big_enough(se_page):
    long_chat(se_page, "(310) 555-0162")
    bar = se_page.get_by_role("navigation", name="Main").bounding_box()
    for field in (
        se_page.get_by_label("Message"),
        se_page.get_by_role("button", name="Send"),
        se_page.get_by_role("link", name="Report"),
        se_page.get_by_role("link", name="Block"),
        se_page.get_by_role("link", name="causematch"),
    ):
        box = field.bounding_box()
        assert box["height"] >= 44, box
    form = se_page.locator("form.cm-composer").bounding_box()
    assert form["y"] + form["height"] <= bar["y"] + 0.5, (form, bar)


@pytest.mark.parametrize("height", [560, 480])
def test_the_chat_composer_sticks_to_the_tab_bar(browser, live_url, height):
    """On a short screen the page scrolls, so only position: sticky keeps the composer
    in view: flush on the tab bar at the top of the page, and above it (never under it)
    wherever the reader scrolls to."""
    ctx = guard(
        browser.new_context(
            viewport={"width": 375, "height": height},
            is_mobile=True,
            has_touch=True,
            base_url=live_url,
        )
    )
    page = ctx.new_page()
    long_chat(page, f"(310) 555-01{height // 10}")
    scroll, inner = page.evaluate(
        "[document.scrollingElement.scrollHeight, innerHeight]"
    )
    assert scroll > inner  # precondition: without sticky the composer would scroll away
    for y in (0, (scroll - inner) // 2, scroll):
        page.evaluate(f"window.scrollTo(0, {y})")
        bar = page.get_by_role("navigation", name="Main").bounding_box()
        form = page.locator("form.cm-composer").bounding_box()
        bottom = form["y"] + form["height"]
        assert form["y"] >= 0 and bottom <= bar["y"] + 1.5, (y, form, bar)
        if y == 0:  # stuck: its own place is below the screen; flush, not overlapping
            assert bar["y"] - 1.5 <= bottom <= bar["y"] + 0.5, (form, bar)
    ctx.close()


def test_the_home_screen_toast_never_covers_the_chat(browser, playwright, live_url):
    ctx = guard(
        browser.new_context(**playwright.devices["iPhone 13"], base_url=live_url)
    )
    page = ctx.new_page()
    long_chat(page, "(310) 555-0163")
    box = page.get_by_label("Message").bounding_box()
    hit = page.evaluate(
        f"document.elementFromPoint({box['x'] + box['width'] / 2}, "
        f"{box['y'] + box['height'] / 2}).getAttribute('aria-label')"
    )
    assert hit == "Message"
    page.goto("/deck")  # the toast's one page: an iPhone in a tab sees it there
    assert page.locator("#a2hs").is_visible()
    ctx.close()


def test_the_current_tab_differs_by_more_than_color(se_page):
    sign_in_page(se_page, "(310) 555-0164")
    se_page.goto("/deck")
    bar = se_page.get_by_role("navigation", name="Main")
    weight = "e => getComputedStyle(e).fontWeight"
    on = bar.locator("[aria-current=page]")
    off = bar.get_by_role("link", name="Matches")
    assert int(on.evaluate(weight)) > int(off.evaluate(weight))
    assert on.locator(".bg-brand").count() == 1  # the bar above the current tab


def test_an_os_dark_mode_keeps_native_controls_light(browser, live_url):
    ctx = guard(browser.new_context(color_scheme="dark", base_url=live_url))
    page = ctx.new_page()
    page.goto("/login")
    scheme = page.evaluate("getComputedStyle(document.documentElement).colorScheme")
    assert scheme == "light"
    ctx.close()


def test_nonprofit_pages_fit_a_small_phone(browser, live_url):
    """Populated, so the widest rows are laid out: an inbox card with Accept/Decline and
    four links, a weekly shift's three buttons, the nav badge and the fill bar."""
    from helpers import coordinator_page

    page = coordinator_page(browser, live_url)
    page.set_viewport_size(SE)
    org_id = orgs()[0].id
    opening = openings(where="org_id = ? AND status = 'open'", where_args=[org_id])[0]
    openings.update(id=opening.id, recurrence="weekly")
    accepted = data.swipe(
        data.get_or_create_volunteer("+13105550171"), opening.id, True
    )
    data.decide(data.coordinator_by_email(coordinator_for(0)[1]), accepted.id, True)
    data.swipe(data.get_or_create_volunteer("+13105550172"), opening.id, True)
    for path, sees in (
        ("/org", "1 waiting for you"),
        ("/org/inbox", "Decline"),
        ("/org/shifts", "Same time next week"),
        ("/org/openings/new", "Post opening"),
        (f"/org/matches/{accepted.id}/shifts/new", "Schedule"),
    ):
        page.goto(path)
        assert sees in page.content(), path
        width = page.evaluate(
            "[document.scrollingElement.scrollWidth, window.innerWidth]"
        )
        assert width[0] <= width[1], (path, width)
        assert page.get_by_role("navigation", name="Nonprofit").is_visible(), path
    page.context.close()


def test_the_homelessness_note_stands_out_only_for_that_cause(browser, live_url):
    """Always shown and read with the checkbox; bold in a highlighted box once the
    cause is homelessness services."""
    from helpers import coordinator_page

    page = coordinator_page(browser, live_url)
    page.goto("/org/openings/new")
    note = page.get_by_text("Homelessness services: group shifts only")
    weight = "e => Number(getComputedStyle(e).fontWeight)"
    box = page.locator(".cm-adult-box")
    border = "e => getComputedStyle(e).borderTopWidth"
    cause = page.locator('select[name="cause"]')
    checkbox = page.locator('input[name="adult_only_ok"]')
    assert note.is_visible() and note.evaluate(weight) < 700
    assert checkbox.get_attribute("aria-describedby") == "homeless-note"
    cause.select_option("homelessness-services")
    assert note.evaluate(weight) >= 700 and box.evaluate(border) == "2px"
    cause.select_option("food-insecurity")
    assert note.evaluate(weight) < 700 and box.evaluate(border) == "1px"
    page.context.close()
