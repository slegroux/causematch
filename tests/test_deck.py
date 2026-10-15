import json

from app import data
from app.db import Opening, now, openings, orgs

SEEN = "Sort and pack pantry boxes"  # positive control: an open, active-cause opening


def add_opening(title, causes, starts_at="2026-12-01T09:00", status="open", **kw):
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
            **kw,
        )
    )


def test_deck_shows_seeded_opening(user):
    r = user.get("/deck")
    assert r.status_code == 200
    assert SEEN in r.text
    assert "Westside Pantry Collective" in r.text
    assert 'name="viewport"' in r.text
    assert r.text.count('name="viewport"') == 1
    assert "<title>Causematch</title>" in r.text


def test_inactive_cause_hidden(user):
    r = user.get("/deck")
    assert r.status_code == 200 and SEEN in r.text
    assert "Pack hygiene kits for outreach" not in r.text


def test_closed_opening_hidden(user):
    r = user.get("/deck")
    assert r.status_code == 200 and SEEN in r.text
    assert "Holiday pantry drive" not in r.text


def test_mixed_cause_opening_shows_only_active_label(user):
    add_opening("Mixed cause shift", ["food-insecurity", "homelessness-services"])
    r = user.get("/deck")
    assert "Mixed cause shift" in r.text
    assert "Food insecurity" in r.text
    assert "Homelessness services" not in r.text


def test_deck_is_soonest_first_and_limited():
    add_opening("Earliest shift", ["food-insecurity"], starts_at="2026-01-01T09:00")
    add_opening("Second shift", ["food-insecurity"], starts_at="2026-01-02T09:00")
    add_opening(
        "Skipped closed",
        ["food-insecurity"],
        starts_at="2025-12-31T09:00",
        status="closed",
    )
    titles = [c["title"] for c in data.deck(limit=2)]
    assert titles == ["Earliest shift", "Second shift"]


def test_null_causes_do_not_break_deck(user):
    add_opening("Bad row", [])
    openings.update(
        id=openings(where="title = ?", where_args=["Bad row"])[0].id, causes=None
    )
    r = user.get("/deck")
    assert r.status_code == 200 and SEEN in r.text


def test_root_redirects_to_deck(user):
    r = user.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307)
    assert r.headers["location"] == "/deck"
