"""A nonprofit claims itself by name, and an admin approves it in one tap."""

import time

import httpx
from conftest import last_link_token
from test_claim import propublica_says, serve_org, serve_search

from app import data, notify
from app.db import claim_requests, orgs
from e2e.helpers import guard


def test_a_coordinator_finds_their_org_by_name_and_never_types_the_ein(
    phone, monkeypatch
):
    propublica_says(monkeypatch, serve_search)
    phone.goto("/org/claim")
    phone.get_by_label("Find your organization").fill("los angeles food")
    phone.get_by_role("button", name="Los Angeles Food Bank Los Angeles, CA").click()
    assert phone.get_by_label("Organization's EIN").input_value() == "86-1620575"
    assert phone.get_by_label("Organization name").input_value() == (
        "Los Angeles Food Bank"
    )
    phone.get_by_label("Your name").fill("Test Claimant")
    phone.get_by_label("Your email").fill("claimant@foodbank.example")
    phone.get_by_text("I'm authorized to represent this organization").click()
    phone.get_by_role("button", name="Request access").click()
    phone.get_by_text("We'll review your request").wait_for()
    (c,) = claim_requests()
    assert (c.ein, c.org_name) == ("861620575", "Los Angeles Food Bank")


def test_when_the_search_fails_the_ein_is_typed_instead(phone, monkeypatch):
    propublica_says(monkeypatch, lambda r: httpx.Response(500))
    phone.goto("/org/claim")
    phone.get_by_label("Find your organization").fill("los angeles food")
    phone.get_by_text("Search isn't working right now").wait_for()
    phone.get_by_label("Organization's EIN").fill("86-1620575")
    assert phone.get_by_label("Organization's EIN").is_editable()


def test_an_admin_approves_a_new_org_in_one_tap_and_the_coordinator_is_emailed(
    browser, live_url, monkeypatch
):
    propublica_says(monkeypatch, serve_org)  # not on Causematch yet: approving adds it
    data.submit_claim(
        "861620575", "Los Angeles Food Bank", "Test Claimant", "claimant@example.org"
    )
    data.add_admin("boss@example.org", "Boss Person")
    ctx = guard(browser.new_context(base_url=live_url))
    page = ctx.new_page()
    page.goto("/org/login")
    page.get_by_label("Email").fill("boss@example.org")
    page.get_by_role("button", name="Email me a link").click()
    deadline = time.monotonic() + 15
    while not notify.get_emailer().sent:
        assert time.monotonic() < deadline, "no sign-in email"
        time.sleep(0.02)
    page.goto(f"/org/login/verify?token={last_link_token()}")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/admin")

    page.goto("/admin/claims")
    card = page.locator("div", has_text="Test Claimant").last
    assert "EIN 86-1620575" in card.inner_text()
    card.get_by_role("button", name="Approve").click()
    page.get_by_text("Approved. We emailed them a link to sign in.").wait_for()
    assert "No pending requests" in page.content()
    to, subject, body = notify.get_emailer().sent[-1]
    assert (to, subject) == ("claimant@example.org", "You're approved on Causematch")
    assert f"Sign in here: {live_url}/org/login" in body
    assert orgs(where="ein = ?", where_args=["861620575"])[0].verified_at
    ctx.close()
