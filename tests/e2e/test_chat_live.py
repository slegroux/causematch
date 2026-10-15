import pytest
from test_swipe import EXTERNAL, blocked, coordinator_page, no_network, sign_in_page

from app import data
from app.db import openings, orgs

pytestmark = pytest.mark.e2e


@pytest.fixture
def phone_context(browser, playwright, live_url):
    blocked.clear()
    ctx = browser.new_context(**playwright.devices["iPhone 13"], base_url=live_url)
    ctx.route(EXTERNAL, no_network)
    yield ctx
    ctx.close()
    # the htmx core, the ws extension and our scripts are all served from this host
    assert not [u for u in blocked if "htmx" in u or "chat.js" in u], blocked


def send(page, text):
    page.get_by_label("Message").fill(text)
    page.get_by_role("button", name="Send").click()


def test_the_coordinator_writes_and_the_volunteer_sees_it_without_reloading(
    phone_context, browser, live_url
):
    vol = phone_context.new_page()
    p = sign_in_page(vol, "(310) 555-0150")
    coord = coordinator_page(browser, live_url)
    opening = openings(where="org_id = ?", where_args=[orgs()[0].id])[0]
    m = data.swipe(p.id, opening.id, True)
    data.decide(data.coordinator_by_email("coord0@example.org"), m.id, True)

    vol.goto(f"/chat/{m.id}")
    coord.goto(f"/org/chat/{m.id}")
    assert vol.viewport_size["width"] == 390
    vol.evaluate("window.sameDocument = 'yes'")

    send(coord, "Hello Sam, see you Saturday")
    vol.get_by_text("Hello Sam, see you Saturday").wait_for()
    coord.get_by_text("Hello Sam, see you Saturday").wait_for()  # their own bubble too
    assert vol.evaluate("window.sameDocument") == "yes"  # no reload happened
    assert coord.get_by_label("Message").input_value() == ""  # the box emptied

    send(vol, "<b>On my way</b>")
    coord.get_by_text("<b>On my way</b>").wait_for()  # shown as typed, not as HTML
    assert coord.locator("#messages b").count() == 0
    assert [x["body"] for x in data.list_messages(p.id, m.id)] == [
        "Hello Sam, see you Saturday",
        "<b>On my way</b>",
    ]
