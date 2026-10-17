"""A nonprofit claims itself by name, and an admin approves it in one tap."""

import httpx
from test_claim import propublica_says, serve_search

from app.db import claim_requests


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
