"""Fixtures every browser test shares. The autouse guard holds each test to the offline,
CSP-enforced page: no request leaves the machine and no inline script runs."""

import pytest

from e2e.helpers import SE, blocked, csp_errors, guard


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
    """An iPhone 13 browser context."""
    ctx = guard(
        browser.new_context(**playwright.devices["iPhone 13"], base_url=live_url)
    )
    yield ctx
    ctx.close()


@pytest.fixture
def phone(browser, live_url):
    """A page on an iPhone SE-sized screen."""
    ctx = guard(
        browser.new_context(
            viewport=SE, is_mobile=True, has_touch=True, base_url=live_url
        )
    )
    yield ctx.new_page()
    ctx.close()
