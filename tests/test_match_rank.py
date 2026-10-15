import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import data
from app.data import LA
from app.db import (
    Match,
    Opening,
    Profile,
    causes,
    db,
    matches,
    now,
    openings,
    orgs,
    profiles,
)

HOME = (34.0560, -118.2365)  # Union Station
FMT = "%Y-%m-%dT%H:%M"


def saturday(hour=9, weeks=2):
    """A Saturday, `weeks` ahead, at `hour` local: weekend_morning for hour < 12."""
    t = datetime.now(LA).replace(hour=hour, minute=0) + timedelta(weeks=weeks)
    return (t + timedelta(days=(5 - t.weekday()) % 7)).strftime(FMT)


def at_km(km_north):
    """A point due north of HOME, about km_north away (1 degree of latitude is 111.2 km)."""
    return HOME[0] + km_north / 111.2, HOME[1]


def add(title, cause="food-insecurity", org=0, where=None, starts_at=None, **kw):
    lat, lon = where or at_km(1)
    fields = {
        "org_id": orgs()[org].id,
        "title": title,
        "about": "",
        "causes": json.dumps([cause]),
        "skills": "[]",
        "lat": lat,
        "lon": lon,
        "neighborhood": "Downtown",
        "remote": False,
        "starts_at": starts_at or saturday(),
        "recurrence": "one_time",
        "slots": 3,
        "status": "open",
        "created_at": now(),
    }
    fields.update(kw)
    return openings.insert(Opening(**fields)).id


@pytest.fixture
def me(user):
    """A volunteer at HOME with an 10 km radius, free Saturday mornings, in a bare world."""
    for o in openings():
        openings.update(id=o.id, status="closed")
    uid = data.get_or_create_volunteer(user.phone)
    profiles.update(
        id=uid,
        lat=HOME[0],
        lon=HOME[1],
        radius_km=10,
        causes='["food-insecurity"]',
        skills='["car"]',
        availability='["weekend_morning"]',
    )
    user.uid = uid
    return user


def titles(me, limit=50):
    return [c["title"] for c in data.deck(me.uid, limit)]


def test_best_fit_ranks_first(me):
    add(
        "far and off-cause",
        cause="animal-shelters",
        where=at_km(9),
        starts_at="2099-01-01T20:00",
    )
    add("cause only", where=at_km(8), starts_at="2099-01-01T20:00")
    add("cause and near", where=at_km(1), starts_at="2099-01-01T20:00")
    add("cause, near, free", where=at_km(1))  # Saturday morning
    assert titles(me) == [
        "cause, near, free",
        "cause and near",
        "cause only",
        "far and off-cause",
    ]


def test_skills_lift_an_opening(me):
    add("no skills asked")
    add("needs a car", skills='["car"]')
    assert titles(me)[0] == "needs a car"


def test_out_of_radius_is_excluded_and_remote_is_included_regardless(me):
    add("inside", where=at_km(9))
    add("outside", where=at_km(11))
    add("remote far away", where=at_km(300), remote=True)
    add("remote no place", where=(None, None), remote=True)
    got = titles(me)
    assert "inside" in got and "outside" not in got
    assert "remote far away" in got and "remote no place" in got


def test_out_of_radius_cannot_be_liked_by_id(me):
    oid = add("outside", where=at_km(11))
    with pytest.raises(LookupError):
        data.swipe(me.uid, oid, True)
    assert matches() == []


def test_inactive_cause_is_excluded(me):
    (dead,) = [c for c in causes() if not c.active][:1]
    add("live", where=at_km(1))
    add("dead cause", cause=dead.slug)
    assert titles(me) == ["live"]


def test_swiped_closed_and_past_are_excluded(me):
    add("keep")
    seen = add("seen")
    add("closed", status="closed")
    add("past", starts_at="2020-01-04T09:00")
    data.swipe(me.uid, seen, False)
    assert titles(me) == ["keep"]


def reply_history(org, decided_fast, decided_slow, silent):
    """Matches on a throwaway closed opening of this org: some answered, some ignored."""
    oid = add("history", org=org, status="closed")
    stamp = lambda hours: (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    rows = []
    for _ in range(decided_fast):
        rows.append(("accepted", stamp(100), stamp(99)))
    for _ in range(decided_slow):
        rows.append(("declined", stamp(100), stamp(10)))  # 90 h later
    for _ in range(silent):
        rows.append(("pending", stamp(100), None))
    for i, (status, created, decided) in enumerate(rows):
        vid = profiles.insert(
            Profile(role="volunteer", phone=f"+1999{org}{oid}{i:03d}")
        ).id
        matches.insert(
            Match(
                opening_id=oid,
                volunteer_id=vid,
                status=status,
                created_at=created,
                decided_at=decided,
            )
        )


def test_slow_replying_org_ranks_below_an_identical_fast_one(me):
    # org 0 answers fast, org 1 leaves its matches pending for days; same opening otherwise
    reply_history(org=0, decided_fast=3, decided_slow=0, silent=0)
    reply_history(org=1, decided_fast=0, decided_slow=1, silent=2)
    add("from slow org", org=1)
    add("from fast org", org=0)  # inserted last, so only the reply rate can lift it
    assert titles(me) == ["from fast org", "from slow org"]


def test_new_org_is_neither_rewarded_nor_punished(me):
    reply_history(org=0, decided_fast=0, decided_slow=0, silent=3)  # 0.0
    reply_history(org=2, decided_fast=3, decided_slow=0, silent=0)  # 1.0
    add("slow", org=0)
    add("new org", org=1)
    add("fast", org=2)
    assert titles(me) == ["fast", "new org", "slow"]


def test_fresh_pending_matches_do_not_count_against_an_org(me):
    oid = add("history", org=0, status="closed")
    vid = profiles.insert(Profile(role="volunteer", phone="+19990000001")).id
    matches.insert(
        Match(opening_id=oid, volunteer_id=vid, status="pending", created_at=now())
    )
    add("from org 0", org=0)
    add("from org 1", org=1)
    assert titles(me) == [
        "from org 0",
        "from org 1",
    ]  # tie on score, org 0's is first by id


def test_deck_order_is_stable(me):
    soon, later = saturday(weeks=2), saturday(weeks=3)
    b = add("b, same fit and time", starts_at=soon)
    a = add("a, same fit and time", starts_at=soon)
    c = add("c, same fit, later", starts_at=later)
    want = [openings[b].title, openings[a].title, openings[c].title]
    assert titles(me) == want  # ties go to the soonest, then the lowest id
    for _ in range(5):
        assert titles(me) == want


def test_limit_takes_the_top_of_the_ranking(me):
    add("meh", cause="animal-shelters")
    add("best")
    assert titles(me, limit=1) == ["best"]


def test_cards_carry_whole_km_and_never_coordinates(me):
    add("six", where=at_km(6.4))
    add("close", where=at_km(0.3))
    add("remote", remote=True)
    cards = {c["title"]: c for c in data.deck(me.uid, 10)}
    assert cards["six"]["distance"] == "6 km"
    assert cards["close"]["distance"] == "< 1 km"
    assert cards["remote"]["distance"] is None and cards["remote"]["remote"]
    for c in cards.values():
        assert not {"lat", "lon", "latitude", "longitude"} & set(c)


def test_pages_say_about_n_km_or_remote(me):
    add("six", where=at_km(6.4))
    assert "about 6 km away" in me.get("/deck").text
    openings.update(
        id=openings(where="title = ?", where_args=["six"])[0].id, remote=True
    )
    assert "Remote" in me.get("/deck").text


def formats(x):
    """Plausible ways a coordinate could leak: repr, fixed decimals, and truncated."""
    full = repr(x)
    return {
        full,
        full.rstrip("0"),
        f"{x:.3f}",
        f"{x:.4f}",
        f"{x:.5f}",
        f"{x:.6f}",
        str(x)[:7],
    }


def test_no_page_html_contains_a_volunteer_or_opening_coordinate(me):
    vlat, vlon = 34.04812, -118.25934
    olat, olon = 34.07391, -118.23127
    profiles.update(id=me.uid, lat=vlat, lon=vlon)
    add("pin one", where=(olat, olon))
    add("pin two", where=(olat + 0.0013, olon - 0.0017))
    pages = [me.get("/deck").text]
    swipe = me.post(
        "/swipe",
        data={
            "opening_id": openings(where="title = ?", where_args=["pin one"])[0].id,
            "liked": "1",
        },
        headers={"HX-Request": "true"},
    )
    pages += [swipe.text, me.get("/matches").text]
    assert (
        "about" in pages[0] and "km away" in pages[0]
    )  # positive control: a card rendered
    secrets = {olat, olon, olat + 0.0013, olon - 0.0017, vlat, vlon}
    for page in pages:
        for x in secrets:
            for f in formats(x):
                assert f not in page, f


@pytest.mark.perf
def test_deck_of_2000_open_openings_in_under_300_ms(me):
    with db.conn:
        for i in range(2000):
            add(
                f"bulk {i}",
                where=at_km(i % 9 + 0.5),
                starts_at=saturday(9 + i % 10, weeks=2 + i % 20),
            )
    assert len(openings(where="status = 'open'")) == 2000
    best = min(_timed(lambda: data.deck(me.uid, 20)) for _ in range(3))
    assert best < 0.3, f"deck took {best * 1000:.0f} ms"


def _timed(fn):
    t = time.perf_counter()
    out = fn()
    assert len(out) == 20
    return time.perf_counter() - t
