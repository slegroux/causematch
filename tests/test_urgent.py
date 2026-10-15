"""Urgent calls: a nonprofit short of people for a shift in the next 48 hours."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import coordinator_for, org_sign_in
from starlette.testclient import TestClient

from app import data
from app.db import matches, openings, orgs, shifts, urgent_calls
from main import app

LA = ZoneInfo("America/Los_Angeles")
DOGS = "Walk dogs at the shelter"  # org 3's opening, 9 days out, 5 spots


def local(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def tomorrow(hhmm="09:00"):
    return f"{(datetime.now(LA) + timedelta(days=1)).date()}T{hhmm}"


def in_hours(h):
    return local(datetime.now(LA) + timedelta(hours=h))


@pytest.fixture
def shelter():
    """Signed-in coordinator of org 3 (Eastside Paws Rescue), with its dog-walking opening."""
    uid, email = coordinator_for(3)
    c = TestClient(app)
    org_sign_in(c, email)
    c.uid = uid
    c.opening = openings(where="title = ?", where_args=[DOGS])[0]
    assert c.opening.org_id == orgs()[3].id
    return c


def post_call(c, starts_at=None, needed="2", auto=False, opening=None):
    body = {"starts_at": starts_at or tomorrow(), "needed": needed}
    if auto:
        body["auto_accept"] = "1"
    oid = (opening or c.opening).id
    return c.post(f"/org/openings/{oid}/urgent", data=body, follow_redirects=False)


def uid_of(user):
    return data.get_or_create_volunteer(user.phone)


def test_post_puts_the_call_first_in_the_deck_with_its_badge(shelter, user):
    before = data.deck(uid_of(user), 50)
    assert before[0]["title"] != DOGS and DOGS in [c["title"] for c in before]
    assert not any(c["urgent"] for c in before)
    r = post_call(shelter)
    assert r.status_code == 303 and r.headers["location"] == "/org"
    (call,) = urgent_calls()
    assert (call.opening_id, call.starts_at, call.needed, call.auto_accept) == (
        shelter.opening.id,
        tomorrow(),
        2,
        False,
    )
    top = data.deck(uid_of(user), 50)[0]
    assert top["title"] == DOGS and top["starts_at"] == tomorrow()
    assert top["urgent"] == "Urgent · tomorrow 9 am · needs 2"
    page = user.get("/deck")
    assert DOGS in page.text and "Urgent · tomorrow 9 am · needs 2" in page.text


def test_deck_today_also_shows_the_call_first(shelter, user):
    post_call(shelter, starts_at=in_hours(30))
    cards = data.deck(uid_of(user), 50, today=True)
    assert cards[0]["title"] == DOGS and cards[0]["urgent"].startswith("Urgent · ")


def test_deck_label_says_today_and_minutes():
    assert data.urgent_label(local(datetime.now(LA)), 1).startswith("Urgent · today ")
    soon = datetime.now(LA) + timedelta(days=1)
    assert (
        data.urgent_label(f"{soon.date()}T14:30", 3)
        == "Urgent · tomorrow 2:30 pm · needs 3"
    )


def test_deck_shows_a_call_on_an_opening_whose_first_shift_passed(shelter, user):
    """A weekly opening started last week: its call brings it back, at the call's time."""
    openings.update(
        id=shelter.opening.id, starts_at=local(datetime.now(LA) - timedelta(days=3))
    )
    assert DOGS not in [c["title"] for c in data.deck(uid_of(user), 50)]
    assert post_call(shelter).status_code == 303
    top = data.deck(uid_of(user), 50)[0]
    assert top["title"] == DOGS and top["starts_at"] == tomorrow()


def test_deck_like_on_an_urgent_card_counts_toward_the_call(shelter, user):
    post_call(shelter)
    r = user.post("/swipe", data={"opening_id": str(shelter.opening.id), "liked": "1"})
    assert r.status_code == 200
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id == urgent_calls()[0].id and m.status == "pending"


def test_deck_like_without_a_call_carries_none(shelter, user):
    user.post("/swipe", data={"opening_id": str(shelter.opening.id), "liked": "1"})
    (m,) = matches(where="opening_id = ?", where_args=[shelter.opening.id])
    assert m.urgent_call_id is None


def test_deck_hides_a_call_from_a_blocked_org(shelter, user):
    """The deck's usual rules hold for an urgent card: a blocked org stays hidden."""
    post_call(shelter)
    uid = uid_of(user)
    data._add_block(blocker_id=uid, blocked_org_id=shelter.opening.org_id)
    assert DOGS not in [c["title"] for c in data.deck(uid, 50)]


@pytest.mark.parametrize("hours", [72, 49, -1])
def test_post_refuses_a_time_outside_the_next_48_hours(shelter, hours):
    r = post_call(shelter, starts_at=in_hours(hours))
    assert r.status_code == 200 and "Pick a time in the next 48 hours." in r.text
    assert not urgent_calls()


def test_post_refuses_a_garbled_time_or_number(shelter):
    for body in (
        {"starts_at": "tomorrow", "needed": "2"},
        {"starts_at": tomorrow(), "needed": "two"},
        {"starts_at": tomorrow(), "needed": "0"},
        {"starts_at": tomorrow(), "needed": "2", "auto_accept": "yes"},
    ):
        r = shelter.post(
            f"/org/openings/{shelter.opening.id}/urgent", data=body,
            follow_redirects=False,
        )  # fmt: skip
        assert r.status_code == 200, body
    assert not urgent_calls()


def test_post_refuses_more_than_the_open_spots(shelter):
    r = post_call(shelter, needed="6")  # 5 spots
    assert "How many more people: a number from 1 to 5." in r.text
    assert not urgent_calls()
    assert post_call(shelter, needed="5").status_code == 303


def test_post_refuses_a_fourth_call_in_a_day(shelter):
    oid = shelter.opening.id
    for _ in range(data.URGENT_PER_DAY):
        assert post_call(shelter).status_code == 303
        stop = shelter.post(f"/org/openings/{oid}/urgent/close", follow_redirects=False)
        assert stop.status_code == 303
    r = post_call(shelter)
    assert r.status_code == 200 and "You can post 3 urgent calls a day." in r.text
    assert len(urgent_calls()) == 3
    # a day later the org may post again
    urgent_calls.update(id=urgent_calls()[0].id, created_at="2020-01-01T00:00:00+00:00")
    assert post_call(shelter).status_code == 303


def test_post_refuses_a_second_open_call_on_one_opening(shelter):
    assert post_call(shelter).status_code == 303
    r = post_call(shelter, starts_at=tomorrow("15:00"))
    assert "already has an urgent call" in r.text
    assert len(urgent_calls()) == 1


def test_post_refuses_a_closed_opening(shelter):
    shelter.post(f"/org/openings/{shelter.opening.id}/close")
    r = post_call(shelter)
    assert r.status_code == 200 and "filled or closed" in r.text
    assert not urgent_calls()


def test_post_page_prefills_a_soon_opening_start(shelter):
    page = shelter.get(f"/org/openings/{shelter.opening.id}/urgent")
    assert page.status_code == 200 and "Urgent call" in page.text
    assert 'value=""' in page.text  # 9 days out: no default time
    openings.update(id=shelter.opening.id, starts_at=tomorrow("10:00"))
    page = shelter.get(f"/org/openings/{shelter.opening.id}/urgent")
    assert f'value="{tomorrow("10:00")}"' in page.text


def test_close_by_the_org_takes_it_out_of_the_deck(shelter, user):
    post_call(shelter)
    dash = shelter.get("/org")
    assert "Urgent · tomorrow 9 am · needs 2" in dash.text
    assert "0 of 2 found" in dash.text and "Stop urgent call" in dash.text
    r = shelter.post(
        f"/org/openings/{shelter.opening.id}/urgent/close", follow_redirects=False
    )
    assert r.status_code == 303
    (call,) = urgent_calls()
    assert call.status == "closed" and call.ended_at
    top = data.deck(uid_of(user), 50)
    assert not any(c["urgent"] for c in top)
    assert "Post an urgent call" in shelter.get("/org").text


def test_close_when_its_time_passes(shelter, user):
    post_call(shelter)
    urgent_calls.update(id=urgent_calls()[0].id, starts_at=in_hours(-1))
    assert not any(c["urgent"] for c in data.deck(uid_of(user), 50))
    assert urgent_calls()[0].status == "closed"
    assert "Stop urgent call" not in shelter.get("/org").text


def test_close_when_filled_books_the_call_time(shelter, user):
    post_call(shelter, needed="1")
    uid = uid_of(user)
    m = data.swipe(uid, shelter.opening.id, True)
    data.decide(shelter.uid, m.id, True)
    call = urgent_calls()[0]
    assert call.status == "filled" and call.ended_at
    (shift,) = shifts(where="match_id = ?", where_args=[m.id])
    assert shift.starts_at == tomorrow()  # the call's shift, not the opening's
    assert not any(c["urgent"] for c in data.deck(uid, 50))


def test_close_when_its_opening_closes(shelter):
    post_call(shelter)
    shelter.post(f"/org/openings/{shelter.opening.id}/close")
    assert urgent_calls()[0].status == "closed"


def test_close_when_its_opening_fills(shelter):
    """Normal likes can fill the opening before the call: the call is filled too."""
    post_call(shelter, needed="1")
    openings.update(id=shelter.opening.id, status="filled")
    assert data.org_urgent_calls(shelter.uid) == {}
    assert urgent_calls()[0].status == "filled"


def test_deck_demo_seeds_an_urgent_call_first_in_the_deck(user):
    """scripts/demo.py's call: on the invented seed it adds the org's second opening."""
    from test_ui import _demo

    coord, _ = coordinator_for(0)
    org = orgs()[0]
    first = openings(where="org_id = ? AND status = 'open'", where_args=[org.id])[0]
    oid = _demo()._urgent_call(coord, org, first)
    assert oid != first.id
    (call,) = urgent_calls()
    assert (call.opening_id, call.needed, call.auto_accept) == (oid, 2, True)
    top = data.deck(uid_of(user), 50)[0]
    assert top["id"] == oid and top["urgent"] == "Urgent · tomorrow 9 am · needs 2"
