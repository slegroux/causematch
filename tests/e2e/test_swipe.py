import re

import pytest
from helpers import coordinator_page

from app.db import matches, openings, profiles

pytestmark = pytest.mark.e2e


TITLE = "Sort and pack pantry boxes"  # the soonest seeded opening, so the top card
EXTERNAL = re.compile(r"^(?!(http|ws)://127\.0\.0\.1)(https?|wss?)://")
blocked = []  # requests that tried to leave the machine
csp_errors = []  # the browser's Content-Security-Policy violation messages


def no_network(route):
    """Every request that leaves the machine gets an empty reply (abort() hangs the
    page load here) and is recorded: every asset is self-hosted, so there must be none."""
    blocked.append(route.request.url)
    route.fulfill(body="")


def guard(ctx):
    """Block the network and record CSP violations in this browser context."""
    ctx.route(EXTERNAL, no_network)
    ctx.on(
        "console",
        lambda m: (
            csp_errors.append(m.text) if "Content Security Policy" in m.text else None
        ),
    )
    return ctx


@pytest.fixture(autouse=True)
def offline_under_csp():
    """Each e2e test runs with the network blocked and the CSP enforced (the server sends
    it), and fails on any external request or CSP violation."""
    blocked.clear()
    csp_errors.clear()
    yield
    assert not blocked, blocked
    assert not csp_errors, csp_errors


@pytest.fixture
def phone_context(browser, playwright, live_url):
    ctx = guard(
        browser.new_context(**playwright.devices["iPhone 13"], base_url=live_url)
    )
    yield ctx
    ctx.close()


def sign_in_page(page, phone):
    page.goto("/login")
    page.get_by_label("Mobile number").fill(phone)
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/onboarding")
    e164 = "+1" + re.sub(r"\D", "", phone)
    (p,) = profiles(where="phone = ?", where_args=[e164])
    profiles.update(id=p.id, display_name="Sam", onboarded_at="2026-01-01T00:00:00")
    return p


def onboard_page(page, phone, where="90012"):
    """Sign in and fill onboarding the way a volunteer does, so the profile has the Home
    place it saves (sign_in_page skips onboarding: a profile from before places)."""
    page.goto("/login")
    page.get_by_label("Mobile number").fill(phone)
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/onboarding")
    page.locator("input[name=first_name]").fill("Sam")
    page.get_by_text("I'm 18 or older").click()
    page.get_by_label("Neighborhood or ZIP").fill(where)
    for slug in ("food-insecurity", "environment-parks", "animal-shelters"):
        page.locator(f"label:has(input[name='cause_{slug}'][value=love])").click()
    page.locator("label:has(input[name=availability][value=sat_morning])").click()
    page.locator("select[name=heard_from]").select_option("public")
    page.get_by_text("I agree", exact=True).click()
    page.get_by_role("button", name="Start swiping").click()
    page.wait_for_url("**/deck")
    e164 = "+1" + re.sub(r"\D", "", phone)
    (p,) = profiles(where="phone = ?", where_args=[e164])
    return p


def touch_drag(page, selector, dx):
    """A real touch swipe through the browser's input pipeline, not synthetic JS events."""
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp = page.context.new_cdp_session(page)

    def send(kind, px):
        points = [{"x": px, "y": y}] if kind != "touchEnd" else []
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

    send("touchStart", x)
    for i in range(1, 11):
        send("touchMove", x + dx * i / 10)
    send("touchEnd", x + dx)


def settle(page):
    """swipe.js ignores input for 400 ms after a card swaps in (a double tap's tail)."""
    page.wait_for_timeout(450)


def test_swipe_right_makes_a_pending_match_the_coordinator_accepts(
    phone_context, browser, live_url
):
    page = phone_context.new_page()
    p = sign_in_page(page, "(310) 555-0142")

    page.goto("/deck")
    assert page.viewport_size["width"] == 390
    page.get_by_text(TITLE).wait_for()
    touch_drag(page, "[data-swipe-card]", 200)
    page.get_by_text("Serve a weekend lunch line").wait_for()  # the next card
    (m,) = matches()
    assert m.status == "pending" and m.volunteer_id == p.id
    settle(page)

    # a short, below-threshold drag does nothing and the card snaps back
    touch_drag(page, "[data-swipe-card]", 20)
    page.wait_for_timeout(200)
    assert page.get_by_text("Serve a weekend lunch line").is_visible()
    assert len(matches()) == 1
    assert page.locator("[data-swipe-card]").evaluate("e => e.style.transform") == ""

    # a left drag passes: no match
    touch_drag(page, "[data-swipe-card]", -200)
    page.get_by_text("Walk dogs at the shelter").wait_for()
    assert len(matches()) == 1

    # the coordinator accepts in their own browser
    coord = coordinator_page(browser, live_url)
    coord.goto("/org/inbox")
    coord.get_by_text("Sam").wait_for()
    coord.get_by_role("button", name="Accept").click()
    coord.wait_for_url("**/org/inbox")

    page.goto("/matches")
    page.get_by_text("Accepted").wait_for()
    assert matches[m.id].status == "accepted"
    assert openings(where="title = ?", where_args=[TITLE])[0].status == "open"


def test_buttons_do_the_same_as_the_drag(phone_context):
    page = phone_context.new_page()
    sign_in_page(page, "(310) 555-0143")
    page.goto("/deck")
    page.get_by_role("button", name="Like").click()
    page.get_by_text("Serve a weekend lunch line").wait_for()
    (m,) = matches()
    assert m.status == "pending"
    # a second tap straight after the swap is the tail of a double tap: ignored
    page.get_by_role("button", name="Like").click()
    page.wait_for_timeout(300)
    assert len(matches()) == 1


def test_mouse_drag_works_too(phone_context):
    page = phone_context.new_page()
    sign_in_page(page, "(310) 555-0144")
    page.goto("/deck")
    box = page.locator("[data-swipe-card]").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 220, y, steps=10)
    page.mouse.up()
    page.get_by_text("Serve a weekend lunch line").wait_for()
    assert [m.status for m in matches()] == ["pending"]


def test_buttons_work_with_javascript_off(browser, live_url):
    ctx = guard(browser.new_context(base_url=live_url, java_script_enabled=False))
    page = ctx.new_page()
    page.goto("/login")
    page.get_by_label("Mobile number").fill("(310) 555-0145")
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()
    (p,) = profiles(where="phone = ?", where_args=["+13105550145"])
    profiles.update(id=p.id, display_name="Jo", onboarded_at="2026-01-01T00:00:00")
    page.goto("/deck")
    page.get_by_role("button", name="Like").click()
    page.get_by_text("Serve a weekend lunch line").wait_for()
    assert [m.volunteer_id for m in matches()] == [p.id]
    ctx.close()


def test_failed_request_snaps_the_card_back(phone_context):
    page = phone_context.new_page()
    sign_in_page(page, "(310) 555-0146")
    page.goto("/deck")
    page.route("**/swipe", lambda r: r.fulfill(status=500, body="boom"))
    page.get_by_text(TITLE).wait_for()
    touch_drag(page, "[data-swipe-card]", 200)
    page.get_by_text("Couldn't save that").wait_for()
    assert page.locator("[data-swipe-card]").evaluate("e => e.style.transform") == ""
    assert page.get_by_text(TITLE).is_visible() and len(matches()) == 0
    page.unroute("**/swipe")
    settle(page)
    page.get_by_role("button", name="Like").click()  # and it is usable again
    page.get_by_text("Serve a weekend lunch line").wait_for()
