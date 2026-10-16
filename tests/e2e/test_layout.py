import pytest
from conftest import close_all_openings, coordinator_for, make_opening

from app import data
from app.db import matches, openings, orgs
from e2e.helpers import SE, accepted_match, coordinator_page, guard, sign_in_page

pytestmark = pytest.mark.e2e


def test_volunteer_pages_fit_a_small_phone_with_the_tab_bar_in_view(phone):
    p = sign_in_page(phone, "(310) 555-0160")
    match_id = accepted_match(p.id)
    for path, tab in (
        ("/deck", "Openings"),
        ("/matches", "Matches"),
        ("/history", "Me"),
        ("/profile", "Me"),
        ("/me/preferences", "Me"),
        ("/me/profile", "Me"),
        ("/profile/delete", "Me"),
        (f"/chat/{match_id}", "Matches"),
        (f"/matches/{match_id}/withdraw", "Matches"),
        (f"/matches/{match_id}/report", "Matches"),
        (f"/matches/{match_id}/block", "Matches"),
    ):
        phone.goto(path)
        width = phone.evaluate(
            "[document.scrollingElement.scrollWidth, window.innerWidth]"
        )
        assert width[0] <= width[1], (path, width)
        bar = phone.get_by_role("navigation", name="Main")
        assert bar.is_visible(), path
        box = bar.bounding_box()
        assert box["y"] + box["height"] == pytest.approx(SE["height"], abs=1), path
        current = bar.locator("[aria-current=page]")
        assert current.inner_text().strip() == tab, path


def test_swipe_buttons_are_big_round_targets(phone):
    sign_in_page(phone, "(310) 555-0161")
    phone.goto("/deck")
    for name in ("Pass", "Like"):
        box = phone.get_by_role("button", name=name).bounding_box()
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


def test_the_chat_composer_and_links_are_big_enough(phone):
    long_chat(phone, "(310) 555-0162")
    bar = phone.get_by_role("navigation", name="Main").bounding_box()
    for field in (
        phone.get_by_label("Message"),
        phone.get_by_role("button", name="Send"),
        phone.get_by_role("link", name="Report"),
        phone.get_by_role("link", name="Block"),
        phone.get_by_role("link", name="causematch"),
    ):
        box = field.bounding_box()
        assert box["height"] >= 44, box
    form = phone.locator("form.cm-composer").bounding_box()
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
    match_id = long_chat(page, "(310) 555-0163")
    box = page.get_by_label("Message").bounding_box()
    hit = page.evaluate(
        f"document.elementFromPoint({box['x'] + box['width'] / 2}, "
        f"{box['y'] + box['height'] / 2}).getAttribute('aria-label')"
    )
    assert hit == "Message"
    data.dismiss_photo_nudge(matches[match_id].volunteer_id)
    page.goto("/deck")  # the toast's one page: an iPhone in a tab sees it there
    assert page.locator("#a2hs").is_visible()
    ctx.close()


def test_the_current_tab_differs_by_more_than_color(phone):
    sign_in_page(phone, "(310) 555-0164")
    phone.goto("/deck")
    bar = phone.get_by_role("navigation", name="Main")
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


def test_the_save_bar_covers_nothing_at_the_bottom_of_preferences(phone):
    sign_in_page(phone, "(310) 555-0163")
    phone.goto("/me/preferences")
    phone.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
    save = phone.get_by_role("button", name="Save preferences").bounding_box()
    bar = phone.get_by_role("navigation", name="Main").bounding_box()
    assert save["y"] + save["height"] <= bar["y"], (save, bar)
    # the last section's last control is above the save button, not under it
    last = phone.locator("label:has(input[name=skills])").last.bounding_box()
    assert last["y"] + last["height"] <= save["y"], (last, save)


def test_a_full_card_keeps_pass_and_like_above_the_tab_bar(phone):
    """The tallest card in practice: a two-line title and place, and four why-chips."""
    from datetime import datetime, timedelta

    from app.db import profiles

    p = sign_in_page(phone, "(310) 555-0164")
    close_all_openings()
    saturday = datetime.now(data.LA) + timedelta(weeks=1)
    saturday += timedelta(days=(5 - saturday.weekday()) % 7)
    make_opening(
        "Sample: Support special events and community outreach",
        causes=["environment-parks"], lat=34.05, lon=-118.25,
        neighborhood="Sepulveda Basin (Encino / Van Nuys)",
        starts_at=saturday.strftime("%Y-%m-%dT09:00"), slots=6, first_timer_ok=True,
        about="You'll help set up the stage, greet guests and hand out programs.",
    )  # fmt: skip
    profiles.update(
        id=p.id,
        lat=34.06,
        lon=-118.24,
        radius_km=60,
        cause_prefs='{"environment-parks": "love"}',
        availability='["sat_morning"]',
    )
    (card,) = data.deck(p.id, 5)
    assert len(card["why"]) == 3  # travel is in the place line
    phone.goto("/deck")
    bar = phone.get_by_role("navigation", name="Main").bounding_box()
    for name in ("Pass", "Like"):
        box = phone.get_by_role("button", name=name).bounding_box()
        assert box["y"] + box["height"] <= bar["y"], (name, box, bar)


def test_onboarding_fills_and_submits_on_a_small_phone(phone):
    phone.goto("/login")
    phone.get_by_label("Mobile number").fill("(310) 555-0165")
    phone.get_by_role("button", name="Text me a code").click()
    phone.get_by_label("Code").fill("000000")
    phone.get_by_role("button", name="Sign in").click()
    phone.wait_for_url("**/onboarding")
    width = phone.evaluate("[document.scrollingElement.scrollWidth, window.innerWidth]")
    assert width[0] <= width[1], width
    phone.locator("input[name=first_name]").fill("Sam")
    phone.get_by_text("I'm 18 or older").click()
    phone.get_by_label("Neighborhood or ZIP").fill("90012")
    phone.locator("label:has(input[name='cause_food-insecurity'][value=love])").click()
    phone.get_by_text("I agree to the").click()
    start = phone.get_by_role("button", name="Start swiping")
    box = start.bounding_box()
    assert box["height"] >= 44 and box["y"] + box["height"] <= SE["height"], box
    start.click()
    phone.wait_for_url("**/deck")


def test_a_new_volunteer_reaches_the_first_card_in_nine_taps_or_fewer(phone):
    """Taps count every click or field focus, from the sign-in screen (the UX audit's count)."""
    taps = 0

    def tap(locator, text=None):
        nonlocal taps
        taps += 1
        locator.click()
        if text is not None:
            locator.fill(text)

    phone.goto("/login")
    tap(phone.get_by_label("Mobile number"), "(310) 555-0167")
    tap(phone.get_by_role("button", name="Text me a code"))
    tap(phone.get_by_label("Code"), "000000")
    tap(phone.get_by_role("button", name="Sign in"))
    phone.wait_for_url("**/onboarding")
    assert phone.locator("input[name=first_name]").evaluate(
        "e => e === document.activeElement"
    )
    phone.keyboard.type("Sam")  # autofocus: no tap needed
    tap(phone.get_by_text("I'm 18 or older"))
    tap(phone.get_by_label("Neighborhood or ZIP"), "90012")
    tap(phone.locator("label:has(input[name='cause_food-insecurity'][value=love])"))
    tap(phone.get_by_text("I agree to the"))
    tap(phone.get_by_role("button", name="Start swiping"))
    phone.wait_for_url("**/deck")
    assert phone.locator("[data-swipe-card]").count() == 1
    assert taps <= 9, taps


def test_pass_and_like_stay_above_the_tab_bar_on_a_short_screen(browser, live_url):
    """A card taller than the space left scrolls under the buttons, which stay sticky
    above the tab bar (static/theme.css .cm-swipe-buttons)."""
    ctx = guard(
        browser.new_context(
            viewport={"width": 375, "height": 440},
            is_mobile=True,
            has_touch=True,
            base_url=live_url,
        )
    )
    page = ctx.new_page()
    sign_in_page(page, "(310) 555-0165")
    page.goto("/deck")
    page.evaluate("window.scrollTo(0, 0)")
    bar = page.get_by_role("navigation", name="Main").bounding_box()
    for name in ("Pass", "Like"):
        box = page.get_by_role("button", name=name).bounding_box()
        assert box["y"] + box["height"] <= bar["y"], (name, box, bar)
    ctx.close()


@pytest.mark.parametrize("device", ["iPhone 13", "iPhone SE"])
def test_the_home_screen_toast_never_covers_pass_like_or_filters(
    browser, playwright, live_url, device
):
    """iPhone Safari in a tab shows the toast on the deck: it sits in the page's flow
    under the header, so a tap on Like, Pass or the filter button reaches it."""
    ctx = guard(browser.new_context(**playwright.devices[device], base_url=live_url))
    page = ctx.new_page()
    p = sign_in_page(page, "(310) 555-0166")
    data.dismiss_photo_nudge(p.id)  # one nudge at a time: the photo prompt comes first
    page.goto("/deck")
    toast = page.locator("#a2hs")
    assert toast.is_visible()  # the precondition: an iPhone in a tab sees it
    targets = {
        "Like": page.get_by_role("button", name="Like"),
        "Pass": page.get_by_role("button", name="Pass"),
        "filters": page.locator("form[action='/swipe']")
        .locator("xpath=..")
        .locator("summary"),
    }
    for y in ("0", "document.scrollingElement.scrollHeight"):
        page.evaluate(f"window.scrollTo(0, {y})")
        shown = toast.bounding_box()
        for name, target in targets.items():
            box = target.bounding_box()
            hit = page.evaluate(
                "([x, y]) => document.elementFromPoint(x, y)"
                ".closest('button, summary')?.outerHTML.slice(0, 80) ?? null",
                [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
            )
            own = target.evaluate("e => e.outerHTML.slice(0, 80)")
            assert hit == own, (device, y, name, hit)
            if shown:  # scrolled off the top is fine; on screen it never overlaps
                apart = (
                    shown["y"] + shown["height"] <= box["y"]
                    or box["y"] + box["height"] <= shown["y"]
                )
                assert apart, (device, y, name, shown, box)
    targets["Like"].click()  # and the tap counts
    page.wait_for_load_state("networkidle")
    assert toast.is_visible()
    ctx.close()
