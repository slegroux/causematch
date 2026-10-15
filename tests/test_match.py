import pytest
from conftest import coordinator_for, org_sign_in, sign_in
from starlette.testclient import TestClient

from app import data
from app.db import matches, openings, orgs, profiles, swipes
from main import app

TITLE = "Sort and pack pantry boxes"  # org 0, status open, 6 slots


def opening(title=TITLE):
    return openings(where="title = ?", where_args=[title])[0]


def volunteer(name="Sam"):
    c = TestClient(app)
    sign_in(c)
    uid = data.get_or_create_volunteer(c.phone)
    profiles.update(id=uid, display_name=name, onboarded_at="2026-01-01T00:00:00")
    c.uid = uid
    return c


def one_slot_opening():
    o = opening()
    openings.update(id=o.id, slots=1)
    return o.id


def test_like_makes_pending_match_and_pass_does_not():
    v = volunteer()
    o1, o2 = (opening(TITLE), opening("Serve a weekend lunch line"))
    m = data.swipe(v.uid, o1.id, True)
    assert m.status == "pending" and m.opening_id == o1.id
    assert data.swipe(v.uid, o2.id, False) is None
    assert [m.opening_id for m in matches()] == [o1.id]
    assert len(swipes()) == 2


def test_double_swipe_is_idempotent_even_with_a_changed_mind():
    v = volunteer()
    o = opening()
    first = data.swipe(v.uid, o.id, True)
    again = data.swipe(v.uid, o.id, True)
    flipped = data.swipe(v.uid, o.id, False)
    assert first.id == again.id == flipped.id
    assert len(matches()) == 1 and len(swipes()) == 1
    p = volunteer()
    assert data.swipe(p.uid, o.id, False) is None
    assert data.swipe(p.uid, o.id, True) is None  # a pass stays a pass
    assert len(matches()) == 1


def test_accept_and_decline_set_status_and_decider():
    v1, v2 = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    m1 = data.swipe(v1.uid, opening().id, True)
    m2 = data.swipe(v2.uid, opening().id, True)
    data.decide(coord, m1.id, True)
    data.decide(coord, m2.id, False)
    got = {m.id: m for m in matches()}
    assert got[m1.id].status == "accepted" and got[m2.id].status == "declined"
    assert got[m1.id].decided_by == coord and got[m1.id].decided_at


def test_double_decide_is_refused():
    v = volunteer()
    coord, _ = coordinator_for(0)
    m = data.swipe(v.uid, opening().id, True)
    data.decide(coord, m.id, True)
    with pytest.raises(ValueError):
        data.decide(coord, m.id, False)
    with pytest.raises(ValueError):
        data.decide(coord, m.id, True)
    assert matches[m.id].status == "accepted"


def test_non_member_and_other_org_coordinator_cannot_decide():
    v = volunteer()
    m = data.swipe(v.uid, opening().id, True)
    other, _ = coordinator_for(1)
    with pytest.raises(PermissionError):
        data.decide(other, m.id, True)
    with pytest.raises(PermissionError):
        data.decide(v.uid, m.id, True)  # the volunteer themselves
    with pytest.raises(PermissionError):
        data.decide(other, 99999, True)
    assert matches[m.id].status == "pending"


def test_full_slots_refuse_accept_and_mark_filled():
    oid = one_slot_opening()
    v1, v2 = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    m1, m2 = data.swipe(v1.uid, oid, True), data.swipe(v2.uid, oid, True)
    data.decide(coord, m1.id, True)
    assert openings[oid].status == "filled"
    with pytest.raises(ValueError):
        data.decide(coord, m2.id, True)
    assert matches[m2.id].status == "pending"
    data.decide(coord, m2.id, False)  # declining is still fine
    assert matches[m2.id].status == "declined"


def test_filled_opening_leaves_the_deck():
    oid = one_slot_opening()
    v1, v2 = volunteer(), volunteer()
    coord, _ = coordinator_for(0)
    data.decide(coord, data.swipe(v1.uid, oid, True).id, True)
    assert oid not in [c["id"] for c in data.deck(v2.uid, limit=50)]
    with pytest.raises(LookupError):
        data.swipe(v2.uid, oid, True)


def test_withdraw_reopens_a_filled_opening():
    oid = one_slot_opening()
    v = volunteer()
    coord, _ = coordinator_for(0)
    m = data.swipe(v.uid, oid, True)
    data.decide(coord, m.id, True)
    assert openings[oid].status == "filled"
    data.withdraw(v.uid, m.id)
    assert matches[m.id].status == "withdrawn" and openings[oid].status == "open"


def test_withdraw_pending_and_only_own_match():
    v, other = volunteer(), volunteer()
    m = data.swipe(v.uid, opening().id, True)
    with pytest.raises(PermissionError):
        data.withdraw(other.uid, m.id)
    data.withdraw(v.uid, m.id)
    assert matches[m.id].status == "withdrawn"
    with pytest.raises(ValueError):
        data.withdraw(v.uid, m.id)
    coord, _ = coordinator_for(0)
    with pytest.raises(ValueError):
        data.decide(coord, m.id, True)  # a withdrawn match can't be accepted


def test_swipe_on_unavailable_openings_is_refused():
    v = volunteer()
    for title in (
        "Holiday pantry drive (past sign-up deadline)",  # closed
        "Pack hygiene kits for outreach",
    ):  # inactive cause
        with pytest.raises(LookupError):
            data.swipe(v.uid, opening(title).id, True)
    with pytest.raises(LookupError):
        data.swipe(v.uid, 99999, True)
    assert len(swipes()) == 0 and len(matches()) == 0


def test_deck_excludes_swiped_openings_per_volunteer():
    v, other = volunteer(), volunteer()
    first = data.deck(v.uid, limit=1)[0]["id"]
    data.swipe(v.uid, first, False)
    assert first not in [c["id"] for c in data.deck(v.uid, limit=50)]
    assert first in [c["id"] for c in data.deck(other.uid, limit=50)]


def test_my_matches_lists_only_own():
    v, other = volunteer(), volunteer()
    data.swipe(v.uid, opening().id, True)
    assert [m["title"] for m in data.my_matches(v.uid)] == [TITLE]
    assert data.my_matches(other.uid) == []


# --- HTTP ---------------------------------------------------------------


def test_deck_shows_one_card_with_buttons(user):
    r = user.get("/deck")
    assert r.text.count("data-swipe-card") == 1
    assert 'hx-post="/swipe"' in r.text and "/static/swipe.js" in r.text
    assert user.get("/static/swipe.js").status_code == 200


def test_swipe_over_htmx_returns_next_card_then_empty_state(user):
    o = opening()
    r = user.post(
        "/swipe",
        data={"opening_id": o.id, "liked": "1"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and TITLE not in r.text and "data-swipe-card" in r.text
    assert len(matches()) == 1
    for _ in range(10):
        left = data.deck(data.get_or_create_volunteer(user.phone), limit=1)
        if not left:
            break
        user.post(
            "/swipe",
            data={"opening_id": left[0]["id"], "liked": "0"},
            headers={"HX-Request": "true"},
        )
    r = user.get("/deck")
    assert "seen every opening" in r.text and "data-swipe-card" not in r.text


def test_swipe_without_htmx_redirects_and_bad_id_makes_no_match(user):
    r = user.post(
        "/swipe", data={"opening_id": 99999, "liked": "1"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/deck"
    assert len(matches()) == 0


def test_matches_page_and_withdraw(user):
    o = opening()
    user.post("/swipe", data={"opening_id": o.id, "liked": "1"})
    r = user.get("/matches")
    assert TITLE in r.text and "Waiting for the nonprofit" in r.text
    mid = matches()[0].id
    r = user.post(f"/matches/{mid}/withdraw", follow_redirects=False)
    assert r.status_code == 303
    assert "Withdrawn" in user.get("/matches").text


def test_other_volunteer_cannot_withdraw_my_match(user):
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    mid = matches()[0].id
    c = volunteer()
    r = c.post(f"/matches/{mid}/withdraw", follow_redirects=False)
    assert r.status_code == 403
    assert matches[mid].status == "pending"


def test_coordinator_inbox_accept_and_volunteer_sees_it(user, coord):
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    mid = matches()[0].id
    r = coord.get("/org/inbox")
    assert "Sam" in r.text and f"/org/matches/{mid}/decide" in r.text
    r = coord.post(
        f"/org/matches/{mid}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Accepted" in user.get("/matches").text
    again = coord.post(f"/org/matches/{mid}/decide", data={"decision": "decline"})
    assert again.status_code == 409


def test_other_org_coordinator_gets_403_and_empty_inbox(user):
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    mid = matches()[0].id
    _, email = coordinator_for(1)
    c = TestClient(app)
    org_sign_in(c, email)
    assert "Sam" not in c.get("/org/inbox").text
    r = c.post(f"/org/matches/{mid}/decide", data={"decision": "accept"})
    assert r.status_code == 403 and matches[mid].status == "pending"


def test_volunteer_cannot_reach_inbox_or_decide(user):
    assert user.get("/org/inbox").status_code == 403
    assert (
        user.post("/org/matches/1/decide", data={"decision": "accept"}).status_code
        == 403
    )


def test_inbox_never_shows_contact_or_location(user, coord):
    uid = data.get_or_create_volunteer(user.phone)
    profiles.update(
        id=uid,
        display_name="Sam Hidden",
        email="sam.secret@example.com",
        lat=34.0123456,
        lon=-118.6543210,
    )
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    mid = matches()[0].id
    coord.post(f"/org/matches/{mid}/decide", data={"decision": "accept"})
    # positive control: the inbox does show the allowed fields
    html = coord.get("/org/inbox").text
    assert "Sam" in html and "Chinatown" in html and "Weekend mornings" in html
    assert "Food insecurity" in html
    for secret in (
        user.phone,
        user.phone[2:],
        "sam.secret",
        "Hidden",
        "34.0123",
        "-118.65",
        "118.6543",
    ):
        assert secret not in html, secret
    assert set(data.org_inbox(orgs() and coord_uid(coord))["decided"][0]) == {
        "id", "status", "opening", "first_name", "causes",
        "availability", "neighborhood", "frequency",
    }  # fmt: skip


def coord_uid(coord):
    return data.coordinator_by_email(f"coord{0}@example.org")
