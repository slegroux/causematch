"""What the e2e modules share besides fixtures (those are in conftest.py): the offline
guard and the sign-in helpers."""

import re
import time

from conftest import coordinator_for, last_link_token

from app import data, notify
from app.db import openings, orgs, profiles

SE = {"width": 375, "height": 667}  # iPhone SE, the smallest phone in common use
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


def _sign_in(page, phone):
    """Sign in by text message; lands on onboarding. Returns the profile row."""
    page.goto("/login")
    page.get_by_label("Mobile number").fill(phone)
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/onboarding")
    e164 = "+1" + re.sub(r"\D", "", phone)
    (p,) = profiles(where="phone = ?", where_args=[e164])
    return p


def sign_in_page(page, phone):
    p = _sign_in(page, phone)
    profiles.update(
        id=p.id,
        display_name="Sam",
        onboarded_at="2026-01-01T00:00:00",
        heard_from="skipped",
    )
    return p


def onboard_page(page, phone, where="90012"):
    """Sign in and fill onboarding the way a volunteer does, so the profile has the Home
    place it saves (sign_in_page skips onboarding: a profile from before places)."""
    p = _sign_in(page, phone)
    page.locator("input[name=first_name]").fill("Sam")
    page.get_by_text("I'm 18 or older").click()
    page.get_by_label("Neighborhood or ZIP").fill(where)
    for slug in ("food-insecurity", "environment-parks", "animal-shelters"):
        page.locator(f"label:has(input[name='cause_{slug}'][value=love])").click()
    page.get_by_text("I agree to the").click()
    page.get_by_role("button", name="Start swiping").click()
    page.wait_for_url("**/deck")
    return p


def coordinator_page(browser, live_url):
    """Signs in as org 0's coordinator in a new browser context and returns its page.

    Waits for the emailed link and for the sign-in to land: a click returns before the server
    has answered, so reading `sent` or navigating straight away races the request."""
    _, email = coordinator_for(0)
    ctx = browser.new_context(base_url=live_url)
    guard(ctx)
    page = ctx.new_page()
    page.goto("/org/login")
    page.get_by_label("Email").fill(email)
    page.get_by_role("button", name="Email me a link").click()
    deadline = time.monotonic() + 15
    while not notify.get_emailer().sent:
        assert time.monotonic() < deadline, "no sign-in email"
        time.sleep(0.02)
    page.goto(f"/org/login/verify?token={last_link_token()}")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/org")
    return page


def accepted_match(uid):
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(uid, opening.id, True)
    data.decide(data.coordinator_by_email(coordinator_for(0)[1]), m.id, True)
    return m.id
