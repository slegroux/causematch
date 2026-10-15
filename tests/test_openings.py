from datetime import datetime, timedelta

import pytest
from conftest import coordinator_for, org_sign_in
from starlette.testclient import TestClient

from app.db import causes, openings, orgs
from main import app


def future(days=10):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def form(oid, **over):
    f = {
        "org_id": str(oid),
        "title": "Saturday fridge restock",
        "about": "Unload and shelve donated produce.",
        "cause": "food-insecurity",
        "zip": "90012",
        "neighborhood": "Chinatown",
        "starts_at": future(),
        "recurrence": "weekly",
        "slots": "5",
        "orientation_required": "no",
        "notes": "Closed-toe shoes.",
        "adult_only_ok": "1",
        "reply_channel": "email",
    }
    f.update(over)
    return f


def post(c, **over):
    return c.post(
        "/org/openings/new", data=form(c.org_id, **over), follow_redirects=False
    )


def test_posted_opening_reaches_deck(coord, user):
    r = post(coord)
    assert r.status_code == 303 and r.headers["location"] == "/org"
    dash = coord.get("/org")
    assert "Saturday fridge restock" in dash.text and "open" in dash.text
    deck = user.get("/deck")
    assert "Saturday fridge restock" in deck.text
    assert "Westside Pantry Collective" in deck.text


def test_closing_removes_it_from_the_deck(coord, user):
    post(coord)
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    assert (
        coord.post(f"/org/openings/{o.id}/close", follow_redirects=False).status_code
        == 303
    )
    assert openings[o.id].status == "closed"
    assert "Saturday fridge restock" not in user.get("/deck").text
    assert "Sort and pack pantry boxes" in user.get("/deck").text  # positive control


def test_stored_opening_fields(coord):
    post(coord)
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    assert (o.org_id, o.status, o.slots, o.recurrence) == (
        coord.org_id,
        "open",
        5,
        "weekly",
    )
    assert o.causes == '["food-insecurity"]' and o.adult_confirmed and o.lat and o.lon


@pytest.mark.parametrize(
    "over",
    [
        {"cause": "homelessness-services"},  # inactive
        {"cause": "nonsense"},
        {"zip": "10001"},
        {"zip": "92101"},
        {"starts_at": "2020-01-01T09:00"},
        {"starts_at": "tomorrow"},
        {"starts_at": ""},
        {"slots": "0"},
        {"slots": "201"},
        {"slots": "five"},
        {"slots": "٣"},
        {"recurrence": "daily"},
        {"orientation_required": "maybe"},
        {"reply_channel": "pigeon"},
        {"adult_only_ok": ""},
        {"title": ""},
        {"title": "x" * 101},
        {"about": "x" * 1001},
        {"notes": "x" * 501},
        {"neighborhood": "x" * 81},
        {"org_id": ""},
        {"org_id": "abc"},
    ],
)
def test_invalid_opening_rejected(coord, over):
    r = post(coord, **over)
    assert r.status_code == 200  # form shown again with an error
    assert "uk-alert" in r.text or 'role="alert"' in r.text
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


def test_inactive_cause_not_offered(coord):
    html = coord.get("/org/openings/new").text
    assert "Food insecurity" in html and "Homelessness services" not in html


def test_cause_deactivated_after_form_loaded_is_rejected(coord):
    causes.update({"slug": "food-insecurity", "active": False})
    assert post(coord).status_code == 200
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


def test_non_string_form_value_is_400(coord):
    r = coord.post(
        "/org/openings/new",
        data={k: v for k, v in form(coord.org_id).items() if k != "title"},
        files={"title": ("a.txt", b"hi")},
    )
    assert r.status_code == 400


def test_no_mass_assignment(coord):
    other = orgs()[1].id
    post(
        coord,
        status="filled",
        id="999",
        lat="1",
        lon="2",
        adult_confirmed="0",
        causes='["x"]',
    )
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    assert o.status == "open" and o.id != 999 and o.org_id == coord.org_id != other
    assert o.lat != 1 and o.causes == '["food-insecurity"]'


def test_org_b_cannot_post_for_org_a(coord):
    _, email_b = coordinator_for(1)
    b = TestClient(app)
    org_sign_in(b, email_b)
    r = b.post("/org/openings/new", data=form(coord.org_id), follow_redirects=False)
    assert r.status_code == 403
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


def test_org_b_cannot_close_org_a_opening(coord):
    post(coord)
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    _, email_b = coordinator_for(1)
    b = TestClient(app)
    org_sign_in(b, email_b)
    assert (
        b.post(f"/org/openings/{o.id}/close", follow_redirects=False).status_code == 403
    )
    assert openings[o.id].status == "open"
    assert (
        b.post("/org/openings/99999/close", follow_redirects=False).status_code == 403
    )


def test_org_b_dashboard_does_not_list_org_a_openings(coord):
    post(coord)
    _, email_b = coordinator_for(1)
    b = TestClient(app)
    org_sign_in(b, email_b)
    html = b.get("/org").text
    assert "Saturday fridge restock" not in html and orgs()[1].name in html


def test_coordinator_of_two_orgs_picks_the_org(coord):
    data_org2 = orgs()[2].id
    from app import data

    data.add_coordinator("coord0@example.org", data_org2)
    html = coord.get("/org/openings/new").text
    assert orgs()[0].name in html and orgs()[2].name in html
    assert (
        coord.post(
            "/org/openings/new", data=form(data_org2), follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        openings(where="title = ?", where_args=["Saturday fridge restock"])[0].org_id
        == data_org2
    )


def test_opening_latlon_never_rendered(coord, user):
    post(coord)
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    pages = [
        coord.get("/org").text,
        coord.get("/org/openings/new").text,
        user.get("/deck").text,
    ]
    for html in pages:
        for v in (o.lat, o.lon, f"{o.lat:.2f}", f"{o.lon:.2f}"):
            assert str(v) not in html
    from app.db import orgs as org_table

    for org in org_table():  # org coordinates too
        assert str(org.lat) not in pages[2]


def test_create_opening_requires_membership():
    from app import data

    uid, _ = coordinator_for(0)
    with pytest.raises(PermissionError):
        data.create_opening(
            uid,
            orgs()[1].id,
            title="t",
            about="a",
            cause="food-insecurity",
            lat=1.0,
            lon=2.0,
            neighborhood="n",
            starts_at=future(),
            recurrence="weekly",
            slots=1,
            orientation_required="no",
            notes="",
            reply_channel="email",
        )
    foreign = next(o for o in openings() if o.org_id != orgs()[0].id)
    with pytest.raises(PermissionError):
        data.close_opening(uid, foreign.id)
