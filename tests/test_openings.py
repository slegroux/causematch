import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import coordinator_for, deck_titles, org_sign_in
from starlette.testclient import TestClient

from app import geo
from app.db import causes, openings, orgs
from main import app

LA = ZoneInfo("America/Los_Angeles")


def future(days=10):
    return (datetime.now(LA) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


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
    r = post(coord, starts_at=future(2))  # soonest, so it is the one card shown
    assert r.status_code == 303 and r.headers["location"] == "/org"
    dash = coord.get("/org")
    assert "Saturday fridge restock" in dash.text and "open" in dash.text
    deck = user.get("/deck")
    assert "Saturday fridge restock" in deck.text
    assert "Westside Pantry Collective" in deck.text


def test_closing_removes_it_from_the_deck(coord, user):
    post(coord, starts_at=future(2))
    assert "Saturday fridge restock" in deck_titles(user)  # positive control
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    assert (
        coord.post(f"/org/openings/{o.id}/close", follow_redirects=False).status_code
        == 303
    )
    assert openings[o.id].status == "closed"
    assert "Saturday fridge restock" not in deck_titles(user)
    assert "Sort and pack pantry boxes" in deck_titles(user)  # positive control


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
        {"zip": "92101"},  # San Diego
        {"zip": "94103"},  # San Francisco
        {"zip": "00000"},
        {"starts_at": future(400)},
        {"starts_at": "2027-03-14T02:30"},  # skipped by the spring-forward clock change
        {"reply_channel": "text"},  # the coordinator has no phone
        {"reply_channel": "either"},
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
def test_invalid_opening_rejected(homelessness_off, coord, over):
    r = post(coord, **over)
    assert r.status_code == 200  # form shown again with an error
    assert "uk-alert" in r.text or 'role="alert"' in r.text
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


def test_inactive_cause_not_offered(homelessness_off, coord):
    html = coord.get("/org/openings/new").text
    assert 'value="food-insecurity"' in html
    assert 'value="homelessness-services"' not in html  # (its note stays, hidden)


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
    post(coord, starts_at=future(2))
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


@pytest.mark.parametrize(
    "zip_code,place",
    [
        ("90012", "Downtown Los Angeles"),
        ("90401", "Santa Monica"),
        ("90232", "Culver City"),
        ("91101", "Pasadena"),
        ("90704", "Avalon"),
    ],
)
def test_any_los_angeles_county_zip_is_accepted_for_an_opening(coord, zip_code, place):
    assert post(coord, zip=zip_code, title=place).status_code == 303
    (o,) = openings(where="title = ?", where_args=[place])
    assert (o.lat, o.lon) == geo.zip_latlon(zip_code)


@pytest.mark.parametrize("zip_code", ["90012-1234", "9001A", "9001", " "])
def test_a_malformed_zip_asks_for_five_digits(coord, zip_code):
    r = post(coord, zip=zip_code)
    assert r.status_code == 200 and "five-digit ZIP code" in r.text
    assert "must be in Los Angeles County" not in r.text  # not blamed on the area
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


@pytest.mark.parametrize("zip_code", ["94103", "00000"])
def test_a_zip_outside_the_county_is_refused_and_says_so(coord, zip_code):
    r = post(coord, zip=zip_code)
    assert r.status_code == 200 and "Los Angeles County" in r.text
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


class _Frozen(datetime):
    """18:00 UTC is 11:00 in Los Angeles (PDT)."""

    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 10, 20, 18, 0, tzinfo=ZoneInfo("UTC"))
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


def test_future_check_uses_los_angeles_time(coord, monkeypatch):
    monkeypatch.setattr("app.routes.org.datetime", _Frozen)
    # UTC wall clock reads 18:00 and LA reads 11:00: only an LA comparison passes both.
    assert post(coord, starts_at="2026-10-20T12:00").status_code == 303  # LA now + 1h
    assert (
        post(coord, starts_at="2026-10-20T10:00", title="Past").status_code == 200
    )  # LA now - 1h
    assert (
        post(coord, starts_at="2026-10-20T17:00", title="Mid").status_code == 303
    )  # before UTC now
    titles = {o.title for o in openings()}
    assert "Past" not in titles and "Mid" in titles


def test_date_window_is_a_year_and_dst_gap_is_rejected(coord, monkeypatch):
    monkeypatch.setattr("app.routes.org.datetime", _Frozen)
    assert post(coord, starts_at="2027-10-19T10:00").status_code == 303
    assert post(coord, starts_at="2027-10-22T10:00", title="Far").status_code == 200
    assert post(coord, starts_at="2027-03-14T02:30", title="Gap").status_code == 200
    assert post(coord, starts_at="2027-03-14T03:30", title="After").status_code == 303


def test_huge_opening_id_is_refused_like_any_unknown_id(coord):
    r = coord.post(
        "/org/openings/99999999999999999999999/close", follow_redirects=False
    )
    assert r.status_code == 403  # the same answer as for an id that does not exist


def test_name_is_required_before_posting(coord):
    from app.db import profiles

    profiles.update(id=profiles(where="role = 'coordinator'")[0].id, display_name="")
    assert "Add your name" in coord.get("/org").text
    assert (
        coord.get("/org/openings/new", follow_redirects=False).headers["location"]
        == "/org"
    )
    assert (
        post(coord).status_code == 303
        and coord.get("/org").text.count("Saturday fridge") == 0
    )
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []
    r = coord.post(
        "/org/details",
        data={"name": "Maria Lopez, coordinator", "phone": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert post(coord).status_code == 303
    assert "Maria Lopez, coordinator" in coord.get("/org").text


def test_details_form_validation_and_text_needs_a_phone(coord):
    from app.db import profiles

    uid = profiles(where="role = 'coordinator'")[0].id
    assert (
        "Text"
        not in coord.get("/org/openings/new")
        .text.split('name="reply_channel"')[1]
        .split("</select>")[0]
    )
    bad = coord.post("/org/details", data={"name": "Maria", "phone": "12345"})
    assert bad.status_code == 200 and "Enter a US phone number" in bad.text
    empty = coord.post("/org/details", data={"name": " ", "phone": ""})
    assert "Enter your name" in empty.text
    assert coord.post("/org/details", files={"name": ("a", b"x")}).status_code == 400
    assert profiles[uid].display_name == "Pat Doe, coordinator"
    assert (
        coord.post(
            "/org/details",
            data={"name": "Maria", "phone": "(310) 555-0123"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert profiles[uid].contact_phone == "+13105550123"
    reply = (
        coord.get("/org/openings/new")
        .text.split('name="reply_channel"')[1]
        .split("</select>")[0]
    )
    assert "Text" in reply and "Either" in reply
    assert post(coord, reply_channel="text").status_code == 303


def test_volunteer_cannot_post_details(user):
    assert (
        user.post(
            "/org/details", data={"name": "x"}, follow_redirects=False
        ).status_code
        == 403
    )


def test_no_inline_handlers_on_org_pages(coord):
    for path in ("/org", "/org/openings/new", "/org/inbox"):
        assert "onclick" not in coord.get(path).text


def test_posted_skills_are_stored_and_ranked(coord, user):
    r = coord.post(
        "/org/openings/new",
        data=form(coord.org_id, skills=["car", "admin"], starts_at=future(2)),
        follow_redirects=False,
    )
    assert r.status_code == 303
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    assert o.skills == '["admin", "car"]'


def test_posted_opening_without_skills_stores_none(coord):
    post(coord)
    (o,) = openings(where="title = ?", where_args=["Saturday fridge restock"])
    assert o.skills == "[]"


@pytest.mark.parametrize(
    "skills", [["telepathy"], ["car", "admin", "media", "physical"], ["car", "bogus"]]
)
def test_unknown_or_too_many_skills_are_rejected(coord, skills):
    r = coord.post(
        "/org/openings/new",
        data=form(coord.org_id, skills=skills),
        follow_redirects=False,
    )
    assert r.status_code == 200 and "wasn't recognized" in r.text
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []


def test_opening_form_offers_the_shared_skills(coord):
    from app.choices import SKILLS

    html = coord.get("/org/openings/new").text
    for _, label in SKILLS:
        assert label in html


def test_every_seeded_opening_skill_is_a_shared_choice():
    from app.choices import SKILLS

    shared = {v for v, _ in SKILLS}
    import json

    for o in openings():
        assert set(json.loads(o.skills)) <= shared, o.title


# --- homelessness services (2026-10-15: active, group shifts only) ---------------------------


NOTE = "Homelessness services: group shifts only"


def test_homelessness_services_is_offered_and_its_opening_reaches_the_deck(coord, user):
    page = coord.get("/org/openings/new").text
    assert "Homelessness services" in page
    # the note is always in the page, read with the checkbox; emphasis waits for the cause
    assert NOTE in page and 'id="homeless-note"' in page
    assert re.search(r'name="adult_only_ok"[^>]*aria-describedby="homeless-note"', page)
    assert not re.search(r'<form[^>]*class="[^"]*\bcm-homeless\b', page)
    r = post(
        coord,
        cause="homelessness-services",
        title="Serve dinner at the shelter",
        starts_at=future(1),
    )
    assert r.status_code == 303
    assert "Serve dinner at the shelter" in deck_titles(user)


def test_a_homelessness_opening_still_needs_the_adults_only_confirmation(coord):
    r = post(coord, cause="homelessness-services", adult_only_ok="")
    assert r.status_code == 200 and ("uk-alert" in r.text or 'role="alert"' in r.text)
    # the redrawn form shows what qualifies, with or without :has()
    assert re.search(r'<form[^>]*class="[^"]*\bcm-homeless\b', r.text)
    assert NOTE in r.text and "background check" in r.text
    assert openings(where="title = ?", where_args=["Saturday fridge restock"]) == []
