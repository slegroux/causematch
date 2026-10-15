"""Design item 7's checks on every phone page, at 375×667: each button and link is a
44 px tap target, no text runs past its box or the screen, and the pages fill in."""

import pytest
from conftest import coordinator_for
from test_layout import SE, accepted_match
from test_swipe import guard, offline_under_csp, sign_in_page  # noqa: F401  (fixture)

from app import data
from app.db import openings, orgs

pytestmark = pytest.mark.e2e

# Visible links, buttons and summaries shorter than 44 px. A link inside a sentence is
# exempt (WCAG 2.5.8): its paragraph holds more text than the link itself.
SMALL = """() => [...document.querySelectorAll('a, button, summary')].filter(e => {
  const r = e.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;           // hidden or sr-only
  if (getComputedStyle(e).visibility === 'hidden') return false;
  if (e.closest('details:not([open]) > :not(summary)')) return false;
  const p = e.parentElement;
  if (e.tagName === 'A' && p && p.textContent.trim().length > e.textContent.trim().length + 2
      && getComputedStyle(e).display === 'inline') return false;  // a link in a sentence
  return r.height < 43.5;
}).map(e => `${e.tagName} "${e.textContent.trim().slice(0, 40)}" ${Math.round(e.getBoundingClientRect().height)}px`)"""

# Text that doesn't fit: a nowrap box whose text is wider than it, or anything past the
# screen's right edge (outside a sideways-scrolling row such as a tab strip).
OVERFLOW = """() => [...document.body.querySelectorAll('*')].filter(e => {
  const r = e.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  const s = getComputedStyle(e);
  if (e.closest('[class*="overflow-x-auto"], .sr-only')) return false;
  const clipped = s.whiteSpace === 'nowrap' && e.scrollWidth > e.clientWidth + 1
                  && s.overflowX === 'visible' && e.children.length === 0;
  return clipped || r.right > innerWidth + 1;
}).map(e => `${e.tagName}.${(e.className || '').toString().slice(0, 50)} "${e.textContent.trim().slice(0, 30)}"`)"""


def check(page, path):
    page.goto(path)
    return page.evaluate(SMALL), page.evaluate(OVERFLOW)


@pytest.fixture
def phone(browser, live_url):
    ctx = guard(
        browser.new_context(
            viewport=SE, is_mobile=True, has_touch=True, base_url=live_url
        )
    )
    yield ctx.new_page()
    ctx.close()


def test_volunteer_and_sign_in_pages_have_big_targets_and_no_overflow(phone):
    found = {}
    for path in ("/login", "/org/login", "/org/claim", "/privacy"):
        found[path] = check(phone, path)
    phone.goto("/login")
    phone.get_by_label("Mobile number").fill("(310) 555-0170")
    phone.get_by_role("button", name="Text me a code").click()
    phone.get_by_label("Code").fill("000000")
    phone.get_by_role("button", name="Sign in").click()
    phone.wait_for_url("**/onboarding")
    found["/onboarding"] = check(phone, "/onboarding")
    phone.context.clear_cookies()
    p = sign_in_page(phone, "(310) 555-0171")
    match_id = accepted_match(p.id)
    for path in (
        "/deck",
        "/matches",
        "/history",
        "/profile",
        "/me/preferences",
        "/me/profile",
        "/profile/delete",
        f"/chat/{match_id}",
        f"/matches/{match_id}/withdraw",
        f"/matches/{match_id}/report",
        f"/matches/{match_id}/block",
    ):
        found[path] = check(phone, path)
    bad = {k: v for k, v in found.items() if v != ([], [])}
    assert bad == {}
    # positive controls: the checks do see a small link and text that runs over
    phone.evaluate(
        """() => {
      const a = document.createElement('a'); a.href = '/x'; a.textContent = 'tiny';
      a.style.display = 'inline-block';
      const s = document.createElement('div'); s.style.whiteSpace = 'nowrap';
      s.style.width = '40px'; s.textContent = 'much too long to fit in forty pixels';
      document.querySelector('main, body').append(a, s);
    }"""
    )
    assert any("tiny" in x for x in phone.evaluate(SMALL))
    assert any("much too long" in x for x in phone.evaluate(OVERFLOW))


def test_nonprofit_pages_have_big_targets_and_no_overflow(browser, live_url):
    from helpers import coordinator_page

    page = coordinator_page(browser, live_url)
    page.set_viewport_size(SE)
    org_id = orgs()[0].id
    opening = openings(where="org_id = ? AND status = 'open'", where_args=[org_id])[0]
    openings.update(id=opening.id, recurrence="weekly")
    m = data.swipe(data.get_or_create_volunteer("+13105550172"), opening.id, True)
    data.decide(data.coordinator_by_email(coordinator_for(0)[1]), m.id, True)
    data.swipe(data.get_or_create_volunteer("+13105550173"), opening.id, True)
    found = {}
    for path in (
        "/org",
        "/org/inbox",
        "/org/shifts",
        "/org/openings/new",
        "/org/organization",
        f"/org/openings/{opening.id}/media",
        f"/org/chat/{m.id}",
        f"/org/matches/{m.id}/shifts/new",
        f"/org/matches/{m.id}/report",
        f"/org/matches/{m.id}/block",
    ):
        found[path] = check(page, path)
    page.context.close()
    bad = {k: v for k, v in found.items() if v != ([], [])}
    assert bad == {}
