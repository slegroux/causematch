"""On Matches an accepted card opens its chat from the title, and the sheet for new
matches closes without script."""

import pytest

from app.db import matches
from e2e.helpers import accepted_match, sign_in_page

pytestmark = pytest.mark.e2e


def test_tapping_an_accepted_cards_title_opens_its_chat(phone):
    p = sign_in_page(phone, "(310) 555-0180")
    match_id = accepted_match(p.id)
    phone.goto("/matches")
    phone.get_by_role("button", name="Close").click()  # the match sheet
    phone.get_by_role("heading", level=2).click()
    phone.wait_for_url(f"**/chat/{match_id}")


def test_the_card_leaves_withdraw_clickable(phone):
    p = sign_in_page(phone, "(310) 555-0181")
    match_id = accepted_match(p.id)
    phone.goto("/matches")
    phone.get_by_role("button", name="Close").click()
    assert phone.locator("dialog").evaluate("d => !d.open")
    phone.get_by_role("link", name="Withdraw").click()
    phone.wait_for_url(f"**/matches/{match_id}/withdraw")
    phone.get_by_role("button", name="Withdraw").click()
    phone.wait_for_url("**/matches")
    assert matches[match_id].status == "withdrawn"
