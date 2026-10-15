import re
import time

from conftest import coordinator_for

from app import notify


def coordinator_page(browser, live_url):
    """Signs in as org 0's coordinator in a new browser context and returns its page.

    Waits for the emailed link and for the sign-in to land: a click returns before the server
    has answered, so reading `sent` or navigating straight away races the request."""
    from test_swipe import EXTERNAL, no_network

    _, email = coordinator_for(0)
    ctx = browser.new_context(base_url=live_url)
    ctx.route(EXTERNAL, no_network)
    page = ctx.new_page()
    page.goto("/org/login")
    page.get_by_label("Email").fill(email)
    page.get_by_role("button", name="Email me a link").click()
    deadline = time.monotonic() + 15
    while not notify.get_emailer().sent:
        assert time.monotonic() < deadline, "no sign-in email"
        time.sleep(0.02)
    _, _, body = notify.get_emailer().sent[-1]
    token = re.search(r"token=([\w-]+)", body).group(1)
    page.goto(f"/org/login/verify?token={token}")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/org")
    return page
