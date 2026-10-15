from starlette.testclient import TestClient

from main import app

client = TestClient(app)


def test_deck_shows_seeded_opening():
    r = client.get("/deck")
    assert r.status_code == 200
    assert "Sort and pack pantry boxes" in r.text
    assert "Westside Pantry Collective" in r.text
    assert 'name="viewport"' in r.text


def test_inactive_cause_hidden():
    assert "Pack hygiene kits for outreach" not in client.get("/deck").text


def test_closed_opening_hidden():
    assert "Holiday pantry drive" not in client.get("/deck").text


def test_root_redirects_to_deck():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307)
    assert r.headers["location"] == "/deck"
