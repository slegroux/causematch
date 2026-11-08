"""What the e2e modules share besides fixtures (those are in conftest.py): the offline
guard and the sign-in helpers."""

import re
import time

from conftest import coordinator_for, last_link_token

from app import data, notify
from app.db import identities, openings, orgs, profiles, shifts

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


def email_of(number):
    """The address a test volunteer signs up with, made from a test's number."""
    return f"v{re.sub(r'\D', '', number)}@example.org"


def open_link(page, email):
    """Wait for the emailed link to this address (sent after the response), then open it:
    a click returns before the server has answered, so reading `sent` straight away races
    the request."""
    deadline = time.monotonic() + 15
    while not any(to == email for to, _, _ in notify.get_emailer().sent):
        assert time.monotonic() < deadline, "no sign-in email"
        time.sleep(0.02)
    page.goto(f"/login/verify?token={last_link_token()}")


def sign_up(page, number):
    """Sign up with the emailed link; lands on onboarding. Returns the profile row."""
    email = email_of(number)
    page.goto("/login")
    page.get_by_label("Email").fill(email)
    page.get_by_role("button", name="Email me a link").click()
    open_link(page, email)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/onboarding")
    (i,) = identities(where="subject = ?", where_args=[email])
    return profiles[i.user_id]


def sign_in_page(page, number):
    p = sign_up(page, number)
    profiles.update(
        id=p.id,
        display_name="Sam",
        onboarded_at="2026-01-01T00:00:00",
        heard_from="skipped",
    )
    return p


def onboard_page(page, number, where="90012"):
    """Sign in and fill onboarding the way a volunteer does, so the profile has the Home
    place it saves (sign_in_page skips onboarding: a profile from before places)."""
    p = sign_up(page, number)
    page.locator("input[name=first_name]").fill("Sam")
    page.get_by_label("Neighborhood or ZIP").fill(where)
    for slug in ("food-insecurity", "environment-parks", "animal-shelters"):
        page.locator(f"label:has(input[name='cause_{slug}'][value=love])").click()
    page.locator("input[name=consent]").check()  # 18+ and the privacy policy, one box
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


def certificate_id(uid, match_id):
    """The id of a certificate for 3 confirmed hours at the match's nonprofit."""
    profiles.update(id=uid, last_name="Lee")
    shifts.insert(
        match_id=match_id,
        starts_at="2026-10-03T09:00",
        status="completed",
        kind="added",
        created_by=uid,
        confirmed_minutes=180,
        marked_by=uid,
        marked_at="2026-10-04T10:00:00+00:00",
    )
    return data.issue_certificate(uid, orgs()[0].id, "all")["id"]
