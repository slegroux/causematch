import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from conftest import coordinator_for

from app import notify
from app.db import matches, openings, profiles
from main import app

pytestmark = pytest.mark.e2e


@pytest.fixture
def base_url():
    """The real app on a free port, in this process so it shares the test DB and the fakes."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()


TITLE = "Sort and pack pantry boxes"  # the soonest seeded opening, so the top card


CDN = re.compile(
    r"^https://"
)  # the glob 'https://**' doesn't match; unrouted CDN loads hang
_htmx = {}


def fetch_htmx(base_url):
    """The htmx script the app's pages link to. Fetched up front: a blocking fetch
    inside a route handler stalls the page load."""
    if not _htmx:
        page = httpx.get(f"{base_url}/login").text
        url = re.search(r'src="([^"]*htmx\.org@[^"]*htmx\.js)"', page).group(1)
        _htmx[url] = httpx.get(url, follow_redirects=True, timeout=30).text
    return _htmx


def cdn(route):
    """Serve htmx, the one CDN script the swipe flow needs, and empty stubs for the
    rest: the flow is behavioural, so unstyled markup is enough, and the test needs no
    other network. (abort() hangs the page load here, so stub instead.)"""
    body = _htmx.get(route.request.url, "")
    route.fulfill(body=body, content_type="application/javascript")


@pytest.fixture
def phone_context(browser, playwright, base_url):
    ctx = browser.new_context(**playwright.devices["iPhone 13"], base_url=base_url)
    fetch_htmx(base_url)
    ctx.route(CDN, cdn)
    yield ctx
    ctx.close()


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


def test_swipe_right_makes_a_pending_match_the_coordinator_accepts(
    phone_context, browser, base_url
):
    page = phone_context.new_page()
    page.goto("/login")
    page.get_by_label("Mobile number").fill("(310) 555-0142")
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/onboarding")
    (p,) = profiles(where="phone = ?", where_args=["+13105550142"])
    profiles.update(id=p.id, display_name="Sam", onboarded_at="2026-01-01T00:00:00")

    page.goto("/deck")
    assert page.viewport_size["width"] == 390
    page.get_by_text(TITLE).wait_for()
    touch_drag(page, "[data-swipe-card]", 200)
    page.get_by_text("Serve a weekend lunch line").wait_for()  # the next card
    (m,) = matches()
    assert m.status == "pending" and m.volunteer_id == p.id

    # a short, below-threshold drag does nothing
    touch_drag(page, "[data-swipe-card]", 20)
    page.wait_for_timeout(300)
    assert page.get_by_text("Serve a weekend lunch line").is_visible()
    assert len(matches()) == 1

    # a left drag passes: no match
    touch_drag(page, "[data-swipe-card]", -200)
    page.get_by_text("Walk dogs at the shelter").wait_for()
    assert len(matches()) == 1

    # the coordinator accepts in their own browser
    _, email = coordinator_for(0)
    coord_ctx = browser.new_context(base_url=base_url)
    coord_ctx.route(CDN, cdn)
    coord = coord_ctx.new_page()
    coord.goto("/org/login")
    coord.get_by_label("Email").fill(email)
    coord.get_by_role("button", name="Email me a link").click()
    _, _, body = notify.get_emailer().sent[-1]
    token = re.search(r"token=([\w-]+)", body).group(1)
    coord.goto(f"/org/login/verify?token={token}")
    coord.get_by_role("button", name="Sign in").click()
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
    page.goto("/login")
    page.get_by_label("Mobile number").fill("(310) 555-0143")
    page.get_by_role("button", name="Text me a code").click()
    page.get_by_label("Code").fill("000000")
    page.get_by_role("button", name="Sign in").click()
    (p,) = profiles(where="phone = ?", where_args=["+13105550143"])
    profiles.update(id=p.id, display_name="Alex", onboarded_at="2026-01-01T00:00:00")
    page.goto("/deck")
    page.get_by_role("button", name="Like").click()
    page.get_by_text("Serve a weekend lunch line").wait_for()
    (m,) = matches()
    assert m.status == "pending"
