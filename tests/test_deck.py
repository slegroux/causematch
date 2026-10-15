import json
from datetime import datetime, timedelta

from conftest import deck_titles

from app import data
from app.data import LA
from app.db import Opening, now, openings, orgs


def soon(days):
    """Local start time `days` from now, so the tests never age out."""
    return (datetime.now(LA) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


SEEN = "Sort and pack pantry boxes"  # positive control: an open, active-cause opening


def add_opening(title, causes, starts_at=None, status="open", **kw):
    starts_at = starts_at or soon(90)
    return openings.insert(
        Opening(
            org_id=orgs()[0].id,
            title=title,
            about="",
            causes=json.dumps(causes),
            skills="[]",
            neighborhood="Sawtelle",
            remote=False,
            starts_at=starts_at,
            slots=3,
            status=status,
            created_at=now(),
            **{"lat": 34.066, "lon": -118.239, **kw},
        )
    )


def test_deck_shows_seeded_opening(user):
    r = user.get("/deck")
    assert r.status_code == 200
    assert SEEN in deck_titles(user)
    assert deck_titles(user)[0] in r.text  # the page renders the top card
    assert "Westside Pantry Collective" in str(
        data.deck(data.get_or_create_volunteer(user.phone), 50)
    )
    assert 'name="viewport"' in r.text
    assert r.text.count('name="viewport"') == 1
    assert "<title>Causematch</title>" in r.text


def test_homelessness_services_openings_are_on_the_deck(user):
    assert "Pack hygiene kits for outreach" in deck_titles(user)


def test_inactive_cause_hidden(homelessness_off, user):
    assert SEEN in deck_titles(user)
    assert "Pack hygiene kits for outreach" not in deck_titles(user)


def test_closed_opening_hidden(user):
    assert SEEN in deck_titles(user)
    assert "Holiday pantry drive" not in deck_titles(user)


def test_mixed_cause_opening_shows_only_active_label(homelessness_off, user):
    add_opening(
        "Mixed cause shift",
        ["food-insecurity", "homelessness-services"],
        starts_at=soon(1),
    )
    uid = data.get_or_create_volunteer(user.phone)
    (card,) = [c for c in data.deck(uid, 50) if c["title"] == "Mixed cause shift"]
    assert card["causes"] == ["Food insecurity"]


def test_equal_fit_is_soonest_first_and_limited(user):
    uid = data.get_or_create_volunteer(user.phone)
    for o in openings():
        openings.update(id=o.id, status="closed")
    sat = datetime.now(LA).replace(hour=9, minute=0) + timedelta(days=7)
    sat += timedelta(
        days=(5 - sat.weekday()) % 7
    )  # a Saturday morning: the user is free
    fmt = "%Y-%m-%dT%H:%M"
    add_opening("Earliest shift", ["food-insecurity"], starts_at=sat.strftime(fmt))
    sat += timedelta(days=7)
    add_opening("Second shift", ["food-insecurity"], starts_at=sat.strftime(fmt))
    add_opening(
        "Skipped closed",
        ["food-insecurity"],
        starts_at=sat.strftime(fmt),
        status="closed",
    )
    titles = [c["title"] for c in data.deck(uid, limit=2)]
    assert titles == ["Earliest shift", "Second shift"]


def test_null_causes_do_not_break_deck(user):
    add_opening("Bad row", [])
    openings.update(
        id=openings(where="title = ?", where_args=["Bad row"])[0].id, causes=None
    )
    assert SEEN in deck_titles(user)


def test_root_redirects_to_deck(user):
    r = user.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307)
    assert r.headers["location"] == "/deck"
