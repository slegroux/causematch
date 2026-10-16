import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import PANTRY, coordinator_for, org_sign_in, uid_of, volunteer
from starlette.testclient import TestClient

from app import data
from app.db import db, matches, openings, places, profiles, swipes
from main import app

FMT = "%Y-%m-%dT%H:%M"


def opening(title=PANTRY):
    return openings(where="title = ?", where_args=[title])[0]


def one_slot_opening():
    o = opening()
    openings.update(id=o.id, slots=1)
    return o.id


def test_like_makes_pending_match_and_pass_does_not():
    v = volunteer()
    o1, o2 = (opening(PANTRY), opening("Serve a weekend lunch line"))
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


def test_swipe_on_unavailable_openings_is_refused(homelessness_off):
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
    assert [m["title"] for m in data.my_matches(v.uid)] == [PANTRY]
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
    assert r.status_code == 200 and PANTRY not in r.text and "data-swipe-card" in r.text
    assert len(matches()) == 1
    for _ in range(10):
        left = data.deck(uid_of(user), limit=1)
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
    assert PANTRY in r.text and "Waiting for the nonprofit" in r.text
    mid = matches()[0].id
    r = user.post(f"/matches/{mid}/withdraw", follow_redirects=False)
    assert r.status_code == 303
    assert "Withdrawn" in user.get("/matches").text


def test_withdraw_asks_first_and_a_get_changes_nothing(user):
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    mid = matches()[0].id
    assert f'href="/matches/{mid}/withdraw"' in user.get("/matches").text
    page = user.get(f"/matches/{mid}/withdraw")
    assert f"Withdraw from {PANTRY}?" in page.text
    assert "You won't be able to like this opening again." in page.text
    assert (
        f'action="/matches/{mid}/withdraw"' in page.text and "onclick" not in page.text
    )
    assert matches[mid].status == "pending"
    user.post(f"/matches/{mid}/withdraw")
    r = user.get(f"/matches/{mid}/withdraw", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/matches"


def test_a_withdrawn_volunteer_cannot_like_the_same_opening_again():
    v = volunteer()
    o = opening()
    m = data.swipe(v.uid, o.id, True)
    data.withdraw(v.uid, m.id)
    again = data.swipe(v.uid, o.id, True)
    assert again.id == m.id and matches[m.id].status == "withdrawn"
    assert len(matches()) == 1


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
    again = coord.post(
        f"/org/matches/{mid}/decide",
        data={"decision": "decline"},
        follow_redirects=False,
    )
    assert again.headers["location"] == "/org/inbox?notice=decided"
    assert "already decided" in coord.get(again.headers["location"]).text
    assert matches[mid].status == "accepted"


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
    uid = uid_of(user)
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
    assert set(data.org_inbox(coordinator_for(0)[0])["decided"][0]) == {
        "id", "status", "opening", "name", "photo_key", "pronouns", "bio",
        "interests", "causes", "availability", "languages", "neighborhood",
        "frequency", "full", "weekly",
    }  # fmt: skip


# --- closed, full and past openings; withdrawals; bad input -----------------


def accept_one(oid, name="Sam"):
    v = volunteer(name)
    coord, _ = coordinator_for(0)
    m = data.swipe(v.uid, oid, True)
    data.decide(coord, m.id, True)
    return v, m


def test_withdraw_from_a_closed_opening_keeps_it_closed():
    oid = opening().id
    v, m = accept_one(oid)
    coord, _ = coordinator_for(0)
    data.close_opening(coord, oid)
    data.withdraw(v.uid, m.id)
    assert openings[oid].status == "closed"
    assert oid not in [c["id"] for c in data.deck(volunteer().uid, limit=50)]


def test_accepting_into_a_closed_opening_keeps_it_closed_even_when_full():
    oid = one_slot_opening()
    v = volunteer()
    coord, _ = coordinator_for(0)
    m = data.swipe(v.uid, oid, True)
    data.close_opening(coord, oid)
    data.decide(coord, m.id, True)
    assert openings[oid].status == "closed"


def test_withdraw_keeps_the_decision_time_and_sets_withdrawn_at():
    v, m = accept_one(opening().id)
    before = matches[m.id]
    assert before.decided_at and not before.withdrawn_at
    data.withdraw(v.uid, m.id)
    after = matches[m.id]
    assert after.decided_at == before.decided_at
    assert after.decided_by == before.decided_by and after.withdrawn_at
    p = volunteer()  # a pending match that is withdrawn was never decided
    pm = data.swipe(p.uid, opening("Serve a weekend lunch line").id, True)
    data.withdraw(p.uid, pm.id)
    assert not matches[pm.id].decided_at and matches[pm.id].withdrawn_at


class _Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def test_past_openings_leave_the_deck_and_cannot_be_liked(monkeypatch):
    v = volunteer()
    first = opening()
    start = datetime.fromisoformat(first.starts_at)
    monkeypatch.setattr(data, "_local_now", _Clock(start.strftime(FMT)))
    assert first.id in [c["id"] for c in data.deck(v.uid, limit=50)]  # starting now
    minute_later = (start + timedelta(minutes=1)).strftime(FMT)
    monkeypatch.setattr(data, "_local_now", _Clock(minute_later))
    assert first.id not in [c["id"] for c in data.deck(v.uid, limit=50)]
    with pytest.raises(LookupError):
        data.swipe(v.uid, first.id, True)
    assert len(matches()) == 0


def test_local_now_is_los_angeles_wall_clock():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    la = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%dT%H")
    assert data._local_now().startswith(la)


@pytest.mark.parametrize("big", ["99999999999999999999", str(2**63)])
def test_ids_past_sqlite_integers_are_refused_not_500(user, coord, big):
    r = user.post("/swipe", data={"opening_id": big, "liked": "1"})
    assert r.status_code < 500
    assert user.post(f"/matches/{big}/withdraw").status_code == 403
    r = coord.post(f"/org/matches/{big}/decide", data={"decision": "accept"})
    assert r.status_code == 403
    assert len(matches()) == 0


@pytest.mark.parametrize("liked", ["yes", "", "2", "true"])
def test_liked_must_be_zero_or_one(user, liked):
    r = user.post("/swipe", data={"opening_id": opening().id, "liked": liked})
    assert r.status_code == 400 and len(swipes()) == 0


def test_full_opening_shows_full_and_a_late_accept_gets_a_notice(user, coord):
    oid = one_slot_opening()
    other = volunteer("Pat")
    data.swipe(other.uid, oid, True)
    user.post("/swipe", data={"opening_id": oid, "liked": "1"})
    first, second = (m.id for m in matches(order_by="id"))
    coord.post(f"/org/matches/{first}/decide", data={"decision": "accept"})
    html = coord.get("/org/inbox").text
    assert "Full" in html and f"/org/matches/{second}/decide" in html
    r = coord.post(
        f"/org/matches/{second}/decide",
        data={"decision": "accept"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/org/inbox?notice=full"
    assert "is full" in coord.get(r.headers["location"]).text
    assert "<h1" in html


def test_swipe_buttons_work_without_htmx(user):
    html = user.get("/deck").text
    assert 'action="/swipe"' in html and 'method="post"' in html
    r = user.post(
        "/swipe", data={"opening_id": opening().id, "liked": "1"}, follow_redirects=True
    )
    assert r.status_code == 200 and "data-swipe-card" in r.text
    assert len(matches()) == 1


def test_no_inline_event_handlers_on_any_page(user, coord):
    import re

    for client, paths in (
        (user, ["/deck", "/matches"]),
        (coord, ["/org", "/org/inbox", "/org/openings/new"]),
        (TestClient(app), ["/login", "/org/login"]),
    ):
        for path in paths:
            html = client.get(path).text
            assert not re.search(r"\son[a-z]+\s*=", html), path
            assert "javascript:" not in html, path


def test_no_unpinned_third_party_scripts():
    html = TestClient(app).get("/login").text
    for bad in ("surreal@main", "css-scope-inline@main", "pico@latest", "htmx.org@"):
        assert bad not in html
    assert "/static/vendor/htmx-2.0.7.min.js" in html
    assert TestClient(app).get("/static/vendor/htmx-2.0.7.min.js").status_code == 200


def test_consent_text_is_what_the_inbox_shows(new_user, coord):
    from html import unescape

    from conftest import FORM

    from app.choices import SHARED_WITH_NONPROFIT

    user = new_user
    onboarding = unescape(user.get("/privacy").text)  # the full wording lives there
    user.post("/onboarding", data=FORM)
    # the consent promises exactly these things ...
    assert SHARED_WITH_NONPROFIT in onboarding
    assert (
        "uses your answers" in onboarding and "to match you with openings" in onboarding
    )
    for promised in (
        "photo",
        "first name and last initial",
        "full last name once they accept you",
        "pronouns",
        "bio",
        "interests",
        "causes",
        "availability",
        "languages",
        "neighborhood",
    ):
        assert promised in SHARED_WITH_NONPROFIT
    assert "Never your phone, email or exact location" in SHARED_WITH_NONPROFIT
    # ... and the inbox shows exactly those, nothing private
    uid = uid_of(user)
    places.delete_where("user_id = ?", [uid])  # placed by its profile columns below
    profiles.update(
        id=uid,
        display_name="Sam Surname",
        email="sam.secret@example.com",
        lat=34.0123456,
        lon=-118.6543210,
        radius_km=60,
        neighborhood="Chinatown",
        causes='["food-insecurity"]',
        availability='["weekend_morning"]',
        frequency="weekly",
        last_name="Lee",
        pronouns="they/them",
        bio="I like early mornings.",
        interests='["cooking"]',
        languages='["es"]',
    )
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    html = coord.get("/org/inbox").text
    for shown in (
        "Sam L.",
        "they/them",
        "I like early mornings.",
        "Cooking",
        "Food insecurity",
        "Weekend mornings",
        "Spanish",
        "Chinatown",
        "weekly",
    ):
        assert shown in html
    for private in (
        user.phone,
        user.phone[2:],
        "sam.secret",
        "Surname",
        "34.0123",
        "118.654",
    ):
        assert private not in html
    (row,) = data.org_inbox(coordinator_for(0)[0])["pending"]
    assert set(row) == {
        "id", "status", "opening", "name", "photo_key", "pronouns", "bio",
        "interests", "causes", "availability", "languages", "neighborhood",
        "frequency", "full", "weekly",
    }  # fmt: skip


def test_org_inbox_is_one_query_per_page_not_per_row():
    coord, _ = coordinator_for(0)
    for i in range(5):
        v = volunteer(f"V{i}")
        data.swipe(v.uid, opening().id, True)
    seen = []

    def trace(cursor, sql, bindings):
        seen.append(sql)
        return True

    db.conn.exec_trace = trace
    try:
        box = data.org_inbox(coord)
    finally:
        db.conn.exec_trace = None
    assert len(box["pending"]) == 5
    assert len([q for q in seen if "FROM match" in q]) == 1, seen


def test_inbox_excludes_other_orgs_and_deleted_volunteers():
    other_coord, _ = coordinator_for(1)
    v, gone = volunteer("Here"), volunteer("Gone")
    data.swipe(v.uid, opening().id, True)
    data.swipe(gone.uid, opening().id, True)
    profiles.update(id=gone.uid, deleted_at="2026-10-15T00:00:00")
    assert data.org_inbox(other_coord) == {"pending": [], "decided": [], "past": []}
    names = [r["name"] for r in data.org_inbox(coordinator_for(0)[0])["pending"]]
    assert names == ["Here"]


# --- concurrency (the data layer shares one apsw connection) ---------------


def run_threads(target, n):
    errors = []
    barrier = threading.Barrier(n)

    def wrap(i):
        try:
            barrier.wait()
            target(i)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not any(t.is_alive() for t in threads)
    return errors


def test_concurrent_swipes_by_different_volunteers_all_succeed():
    vols = [volunteer(f"V{i}") for i in range(12)]
    oid = opening().id
    errors = run_threads(lambda i: data.swipe(vols[i].uid, oid, True), 12)
    assert errors == [] and len(matches()) == 12


def test_concurrent_decide_withdraw_and_sign_in_do_not_collide():
    oid = one_slot_opening()
    vols = [volunteer(f"V{i}") for i in range(6)]
    ms = [data.swipe(v.uid, oid, True) for v in vols]
    coord, _ = coordinator_for(0)

    def work(i):
        if i < 6:
            try:
                data.decide(coord, ms[i].id, True)
            except ValueError:
                pass  # lost the race for the one slot
        elif i < 12:
            try:
                data.withdraw(vols[i - 6].uid, ms[i - 6].id)
            except ValueError:
                pass
        else:
            data.get_or_create_volunteer(f"+1310555{9000 + i}")

    errors = run_threads(work, 18)
    assert errors == []
    assert data._accepted(oid) <= 1


def test_twenty_concurrent_swipes_over_http_with_sign_ins_in_flight(live_url):
    import httpx

    def login(phone):
        c = httpx.Client(base_url=live_url, headers={"Origin": live_url})
        c.post("/login/send", data={"phone": phone})
        r = c.post("/login/verify", data={"code": "000000"})
        return c, r

    clients = []
    for i in range(20):
        c, r = login(f"310555{8000 + i}")
        assert r.status_code == 303 or r.status_code == 200
        uid = data.get_or_create_volunteer(f"+1310555{8000 + i}")
        profiles.update(
            id=uid, display_name=f"V{i}", onboarded_at="2026-01-01T00:00:00"
        )
        clients.append(c)
    oid = opening().id
    codes = []

    def work(i):
        if i < 20:
            r = clients[i].post(
                "/swipe",
                data={"opening_id": oid, "liked": "1"},
                headers={"HX-Request": "true"},
            )
        else:
            _, r = login(f"310555{7000 + i}")
        codes.append(r.status_code)

    errors = run_threads(work, 28)
    assert errors == [] and max(codes) < 500, codes
    assert len(matches()) == 20
    for c in clients:
        c.close()


# --- the name an org sees: "Sam L.", then "Sam Lee" once accepted --------------------------


def test_the_org_sees_the_last_initial_until_it_accepts(user, coord):
    uid = uid_of(user)
    profiles.update(id=uid, last_name="Lee", pronouns="they/them")
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    mid = matches()[0].id
    inbox = coord.get("/org/inbox").text
    assert "Sam L." in inbox and "Lee" not in inbox and "they/them" in inbox
    assert "Report Sam L." in coord.get(f"/org/matches/{mid}/report").text
    coord.post(f"/org/matches/{mid}/decide", data={"decision": "accept"})
    assert "Sam Lee" in coord.get("/org/inbox").text
    assert "Sam Lee" in coord.get(f"/org/chat/{mid}").text
    assert "Sam Lee" in coord.get("/org/shifts").text
    data.withdraw(uid, mid)
    past = coord.get("/org/inbox").text
    assert "Sam L." in past and "Sam Lee" not in past


def test_a_declined_volunteer_keeps_the_initial(user, coord):
    uid = uid_of(user)
    profiles.update(id=uid, last_name="Lee")
    user.post("/swipe", data={"opening_id": opening().id, "liked": "1"})
    coord.post(f"/org/matches/{matches()[0].id}/decide", data={"decision": "decline"})
    html = coord.get("/org/inbox").text
    assert "Sam L." in html and "Lee" not in html


def test_in_production_a_like_stays_pending(user, monkeypatch):
    """The demo's auto-accept (data.demo_accept_due) never runs outside a demo, even for an
    org whose coordinator is a demo stand-in."""
    uid = uid_of(user)
    card = data.deck(uid, 1)[0]
    org_id = openings[card["id"]].org_id
    data.add_coordinator(f"team@{data.DEMO_TEAM_DOMAIN}", org_id, name="Rosa")
    secret = "s" * 32
    for k, v in {
        "CAUSEMATCH_ENV": "production",
        "PHONE_HASH_KEY": secret,
        "TASKS_TOKEN": secret,
        "CONTACT_EMAIL": "x@causematch.test",
    }.items():
        monkeypatch.setenv(k, v)  # fmt: skip
    user.post("/swipe", data={"opening_id": card["id"], "liked": "1"})
    (m,) = matches(where="volunteer_id = ?", where_args=[uid])
    matches.update(id=m.id, created_at=(datetime.now(UTC) - timedelta(minutes=1))
                   .isoformat(timespec="seconds"))  # fmt: skip
    assert user.get("/deck").status_code == user.get("/matches").status_code == 200
    assert matches[m.id].status == "pending" and not db.q("SELECT id FROM message")
    monkeypatch.setenv("CAUSEMATCH_DEMO_MATCHES", "1")  # refused at startup; here too
    with pytest.raises(RuntimeError):
        data.demo_accept_due(uid)
    assert matches[m.id].status == "pending"
