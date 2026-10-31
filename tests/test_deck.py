import re
from datetime import datetime, timedelta

from conftest import (
    close_all_openings,
    coordinator_for,
    deck_titles,
    make_opening,
    uid_of,
)

from app import data
from app.data import LA
from app.db import matches, openings, profiles

HOME_PLACES = (
    True  # each test runs with a saved Home place and as a profile from before
)


def soon(days):
    """Local start time `days` from now, so the tests never age out."""
    return (datetime.now(LA) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


SEEN = "Sort and pack pantry boxes"  # positive control: an open, active-cause opening


def add_opening(title, causes, starts_at=None, **kw):
    return make_opening(
        title,
        **{
            "causes": causes,
            "lat": 34.066,
            "lon": -118.239,
            "neighborhood": "Sawtelle",
            "starts_at": starts_at or soon(90),
            **kw,
        },
    )


def test_deck_shows_seeded_opening(user):
    r = user.get("/deck")
    assert r.status_code == 200
    assert SEEN in deck_titles(user)
    assert deck_titles(user)[0] in r.text  # the page renders the top card
    assert "Westside Pantry Collective" in str(data.deck(uid_of(user), 50))
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
    uid = uid_of(user)
    (card,) = [c for c in data.deck(uid, 50) if c["title"] == "Mixed cause shift"]
    assert card["causes"] == ["Food insecurity"]


def test_equal_fit_is_soonest_first_and_limited(user):
    uid = uid_of(user)
    close_all_openings()
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


def test_card_clamps_about_and_its_sheet_holds_the_rest(user):
    close_all_openings()
    add_opening(
        "Greet guests",
        ["food-insecurity"],
        about="We greet guests at the door and walk them to a table.",
        notes="Bring closed shoes.",
        orientation_required="same_day",
    )
    html = user.get("/deck").text
    assert "line-clamp-2" in html and "We greet guests at the door" in html
    assert ">More<" in html  # what the clamp hides is a tap away
    assert re.search(
        r"text-center[^>]*>1 opening<", html
    )  # the count under Pass and Like
    assert "<dialog" in html and "Bring closed shoes." in html
    assert "Orientation: done the same day" in html


def test_a_like_says_where_it_went_and_does_not_light_the_matches_badge(user):
    close_all_openings()
    oid = add_opening("Greet guests", ["food-insecurity"]).id
    r = user.post(
        "/swipe", data={"opening_id": str(oid), "liked": "1"},
        headers={"HX-Request": "true"},
    )  # fmt: skip
    org = data.org_name_of(oid)
    assert f"When {org} says yes" in r.text and "text you" in r.text
    dot = re.search(r'<span[^>]*id="matches-dot"[^>]*>', r.text).group(0)
    assert "hidden" in dot and 'hx-swap-oob="true"' in dot  # a pending like: nothing
    assert '<span hidden id="matches-dot"' in user.get("/deck").text


def test_the_matches_badge_counts_decisions_since_the_last_look(user):
    close_all_openings()
    coord = coordinator_for(0)[0]
    won = data.swipe(uid_of(user), add_opening("Won one", ["food-insecurity"]).id, True)
    lost = data.swipe(
        uid_of(user), add_opening("Lost one", ["food-insecurity"]).id, True
    )
    badge = lambda page: re.search(
        r'<span( hidden|) id="matches-dot"[^>]*>([^<]*)', user.get(page).text
    ).groups()
    assert badge("/deck") == (" hidden", "0")  # both still waiting
    profiles.update(id=uid_of(user), matches_seen_at="2000-01-01T00:00:00+00:00")
    data.decide(coord, won.id, True)
    data.decide(coord, lost.id, False)
    assert badge("/deck") == ("", "2")  # the deck's tab bar, on any volunteer page
    user.get("/matches")  # they look
    assert badge("/deck") == (" hidden", "0")


def test_one_nudge_at_a_time_photo_first_then_home_screen(user):
    uid = uid_of(user)
    assert 'id="a2hs"' not in user.get("/deck").text  # the photo prompt is up
    data.dismiss_photo_nudge(uid)
    html = user.get("/deck").text
    assert 'id="a2hs"' in html and "Add a photo so nonprofits" not in html


def _accepted(user, title, at):
    """A like of `title` the org said yes to at `at` (a UTC ISO time)."""
    m = data.swipe(uid_of(user), add_opening(title, ["food-insecurity"]).id, True)
    data.decide(coordinator_for(0)[0], m.id, True)
    matches.update(id=m.id, decided_at=at)
    return m.id


def test_new_matches_show_in_one_sheet_once(user):
    close_all_openings()
    ids = [
        _accepted(user, f"Match {n}", f"2030-01-01T00:00:0{n}+00:00") for n in (1, 2, 3)
    ]
    for page in ("/matches", "/deck"):
        profiles.update(id=uid_of(user), match_moment_seen_at=None)
        html = user.get(page).text
        assert html.count("<dialog") == 1 and "s a match!" in html
        for n, i in enumerate(ids, 1):
            assert f'href="/chat/{i}"' in html and f"Match {n}" in html
        assert "<dialog" not in user.get(page).text  # a reload: seen
    later = _accepted(user, "Match 4", "2030-01-02T00:00:00+00:00")
    html = user.get("/deck").text
    assert html.count("<dialog") == 1 and f'href="/chat/{later}"' in html
    assert "Match 4" in html and "Match 1" not in html and "Match 3" not in html


def test_a_card_near_a_booked_shift_warns_and_the_other_does_not(user):
    close_all_openings()
    sat = datetime.now(LA) + timedelta(days=(5 - datetime.now(LA).weekday()) % 7 + 14)
    day = sat.strftime("%Y-%m-%d")
    booked = add_opening("Booked one", ["food-insecurity"], f"{day}T09:00")
    m = data.swipe(uid_of(user), booked.id, True)
    data.decide(coordinator_for(0)[0], m.id, True)
    add_opening("Near one", ["food-insecurity"], f"{day}T10:00")
    add_opening("Far one", ["food-insecurity"], f"{day}T14:00")
    cards = {c["title"]: c for c in data.deck(uid_of(user))}
    texts = lambda t: [w["text"] for w in cards[t]["why"]]
    assert any(w.startswith("You're booked at ") for w in texts("Near one"))
    assert not any("booked" in w for w in texts("Far one"))


def test_an_urgent_card_keeps_its_clash_chip_past_the_cap_and_a_full_week(user):
    close_all_openings()
    day = (datetime.now(LA) + timedelta(days=1)).strftime("%Y-%m-%d")
    coord = coordinator_for(0)[0]
    booked = add_opening("Booked one", ["food-insecurity"], f"{day}T09:00")
    data.decide(coord, data.swipe(uid_of(user), booked.id, True).id, True)
    call = add_opening("Call one", ["food-insecurity", "environment-parks"])
    data.post_urgent_call(coord, call.id, f"{day}T10:00", 1, True)

    def chips():
        [card] = [c for c in data.deck(uid_of(user)) if c["title"] == "Call one"]
        return [w["text"] for w in card["why"]]

    assert len(chips()) == data.URGENT_WHY
    assert chips()[-1].startswith("You're booked at ")  # 2 causes, still shown
    profiles.update(id=uid_of(user), weekly_limit=1)  # that one shift fills the week
    *_, clash, week = chips()
    assert clash.startswith("You're booked at ") and week == data.WEEK_FULL["text"]


def _decline(user, title):
    m = data.swipe(uid_of(user), add_opening(title, ["food-insecurity"]).id, True)
    data.decide(coordinator_for(0)[0], m.id, False)
    return m.id


def test_matches_page_keeps_matches_likes_and_past_apart(user):
    close_all_openings()
    _accepted(user, "Won one", "2030-01-01T00:00:00+00:00")
    data.swipe(uid_of(user), add_opening("Waiting one", ["food-insecurity"]).id, True)
    _decline(user, "Declined one")
    html = user.get("/matches").text
    assert ">Matches</h2>" in html and ">Your likes</h2>" in html
    assert (
        html.index("Won one") < html.index(">Your likes<") < html.index("Waiting one")
    )
    past = html[html.index("<details", html.index("Waiting one")) :]
    assert ">Past<" in past and "Declined one" in past
    assert (
        "Declined one" not in html[: html.index("<details", html.index("Waiting one"))]
    )


def test_matches_page_with_only_likes_says_no_matches_yet(user):
    close_all_openings()
    data.swipe(uid_of(user), add_opening("Waiting one", ["food-insecurity"]).id, True)
    html = user.get("/matches").text
    assert "No matches yet. When a nonprofit says yes" in html
    assert ">Your likes</h2>" in html and "<details" not in html.split("Waiting one")[0]


def test_the_match_takeover_names_the_org_and_both_faces(user):
    close_all_openings()
    mid = _accepted(user, "Won one", "2030-01-01T00:00:00+00:00")
    profiles.update(id=uid_of(user), match_moment_seen_at=None, display_name="Robin")
    html = user.get("/deck").text
    sheet = html[html.index("<dialog") : html.index("</dialog>")]
    assert "s a match!" in sheet and "said yes" in sheet and "Won one" in sheet
    assert f'href="/chat/{mid}"' in sheet and "Say hi" in sheet
    assert "Keep swiping" in sheet and 'method="dialog"' in sheet
    assert ">R</span>" in sheet  # the volunteer's own initial: no photo yet


def test_deck_news_brings_a_fresh_match_once_and_nothing_after(user):
    close_all_openings()
    htmx = {"HX-Request": "true"}
    quiet = user.get("/deck/news", headers=htmx)
    assert "<dialog" not in quiet.text and quiet.headers["hx-reswap"] == "none"
    assert 'id="matches-dot"' in quiet.text  # the dot still updates
    mid = _accepted(user, "Won one", "2030-01-01T00:00:00+00:00")
    profiles.update(id=uid_of(user), match_moment_seen_at=None)
    news = user.get("/deck/news", headers=htmx)
    assert "<dialog" in news.text and f'href="/chat/{mid}"' in news.text
    assert "hx-reswap" not in news.headers
    again = user.get("/deck/news", headers=htmx)
    assert "<dialog" not in again.text and again.headers["hx-reswap"] == "none"


def test_deck_and_matches_pages_poll_for_news(user):
    for page in ("/deck", "/matches"):
        html = user.get(page).text
        assert 'id="moment-slot"' in html and 'hx-get="/deck/news"' in html
