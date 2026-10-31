"""/me/preferences: three-state causes, the answers from onboarding made editable, and the
migration that turns picked causes into loved ones."""

import importlib
import itertools
import json
import random
from datetime import datetime

import apsw
import pytest
from conftest import close_all_openings, deck_titles, make_opening, uid_of

from app import data, match
from app.db import causes, db, openings, profiles

cause_prefs_migration = importlib.import_module("app.migrations.0010_cause_prefs")
day_slots_migration = importlib.import_module("app.migrations.0011_day_slots")

FOOD, PARKS, ANIMALS = "food-insecurity", "environment-parks", "animal-shelters"
SAVE = {
    "sections": ["when", "work"],  # as the pages send: their checkboxes count
    f"cause_{FOOD}": "love",
    f"cause_{PARKS}": "open",
    f"cause_{ANIMALS}": "no",
    "availability": ["tue_evening", "sat_morning"],
    "skills": ["admin", "media"],
    "frequency": "monthly",
    # the sign-up place, unchanged: conftest's FORM says Chinatown
    "place1_label": "Home",
    "place1_where": "Chinatown",
    "place1_mode": "car",
    "place1_minutes": "0",  # any distance, as the user fixture widened it
}


def profile_of(client):
    return profiles[uid_of(client)]


def answers(client):
    p = profile_of(client)
    return p.cause_prefs, p.causes, p.availability, p.skills, p.frequency


def add(title, cause=FOOD, km=1.0, starts_at="2099-01-03T09:00", **kw):
    """An open opening about km north of the volunteer (see `bare`)."""
    return make_opening(
        title,
        **{
            "causes": cause if isinstance(cause, list) else [cause],
            "lat": 34.0560 + km / 111.195,
            "lon": -118.2365,
            "neighborhood": "Downtown",
            "starts_at": starts_at,
            **kw,
        },
    ).id


@pytest.fixture
def bare(user):
    """The onboarded user at Union Station with a 30 km radius, and no seeded openings."""
    close_all_openings()
    profiles.update(id=uid_of(user), lat=34.0560, lon=-118.2365, radius_km=30)
    return user


# --- the page --------------------------------------------------------------------------------


def test_me_links_to_preferences(user):
    assert 'href="/me/preferences"' in user.get("/profile").text


def test_page_shows_the_onboarding_answers(homelessness_off, user):
    index = user.get("/me/preferences").text
    assert "Preferences" in index and "Only you see them" in index
    # FORM in conftest: food loved, free weekend mornings, has a car, weekly
    for page, name, value in (
        ("causes", f"cause_{FOOD}", "love"),
        ("when", "availability", "sat_morning"),  # weekend mornings, as day slots
        ("when", "availability", "sun_morning"),
        ("work", "skills", "car"),
        ("work", "frequency", "weekly"),
    ):
        html = user.get(f"/me/preferences/{page}").text
        assert f'name="{name}" value="{value}" checked' in html, name
    html = user.get("/me/preferences/causes").text
    assert f'name="cause_{PARKS}" value="love" checked' not in html
    assert "Homelessness services" not in html  # inactive causes are not offered


def details_of(html, summary):
    """The opening tag of the <details> whose summary text starts with summary."""
    head = html[: html.index(summary)]
    return head[head.rindex("<details") :]


def test_a_saved_second_place_stays_folded_and_says_what_it_is(user):
    user.post(
        "/me/preferences",
        data={
            **SAVE,
            "place2_where": "Venice",
            "place2_mode": "walk",
            "place2_minutes": "15",
        },
    )
    html = user.get("/me/preferences/causes").text
    assert "Your second place" in html and "Venice · Walk, up to 15 min" in html
    assert "Add a second place" not in html
    assert "open" not in details_of(html, "Your second place").split(">")[0].split()


def test_without_a_second_place_the_fold_invites_one(user):
    html = user.get("/me/preferences/causes").text
    assert "Add a second place" in html and "Your second place" not in html


def test_a_bad_second_zip_reopens_the_fold(user):
    r = user.post(
        "/me/preferences",
        data={
            **SAVE,
            "sections": "causes",
            "back": "causes",
            "place2_where": "1234",
            "place2_mode": "car",
            "place2_minutes": "30",
        },
    )
    assert "Enter a five-digit ZIP code" in r.text
    assert "open" in details_of(r.text, "Your second place").split(">")[0].split()


@pytest.mark.parametrize(
    "key, marker",
    [("causes", "causes"), ("when", "when"), ("work", "work"), ("urgent", "urgent")],
)
def test_every_section_page_has_its_own_save_and_marker(user, key, marker):
    assert f'href="/me/preferences/{key}"' in user.get("/me/preferences").text
    r = user.get(f"/me/preferences/{key}")
    assert r.status_code == 200 and "Save</button>" in r.text
    assert f'value="{marker}" name="sections"' in r.text
    assert f'value="{key}" name="back"' in r.text
    assert user.get("/me/preferences/nowhere").status_code == 404


def test_saving_one_section_leaves_the_others_unchanged(user):
    user.post(
        "/me/preferences",
        data={
            **SAVE,
            "urgent_on": "1",
            "sections": ["when", "work", "urgent"],
            "urgent_window": "today",
            "urgent_quiet_from": "22",
            "urgent_quiet_to": "7",
            "urgent_per_week": "2",
            "place2_where": "Venice",
            "place2_mode": "walk",
            "place2_minutes": "15",
        },
    )
    before = data.preferences(uid_of(user))
    r = user.post(
        "/me/preferences",
        data={"sections": "when", "back": "when", "availability": ["mon_morning"]},
        follow_redirects=False,
    )
    assert (
        r.status_code == 303 and r.headers["location"] == "/me/preferences/when?saved=1"
    )
    after = data.preferences(uid_of(user))
    assert after["availability"] == ["mon_morning"]
    for k in ("causes", "places", "skills", "languages", "urgent", "frequency"):
        assert after[k] == before[k], k
    r = user.post(
        "/me/preferences",
        data={"sections": "work", "back": "work"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/me/preferences/work?saved=1"
    again = data.preferences(uid_of(user))
    assert again["availability"] == ["mon_morning"]
    assert (
        again["skills"] == [] and again["frequency"] == ""
    )  # this page's own, cleared
    assert again["places"] == before["places"] and again["causes"] == before["causes"]


def test_saving_stores_every_answer(user):
    r = user.post(
        "/me/preferences", data={**SAVE, "back": "causes"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/me/preferences/causes?saved=1"
    p = profile_of(user)
    assert json.loads(p.cause_prefs) == {FOOD: "love", PARKS: "open", ANIMALS: "no"}
    assert json.loads(p.causes) == [FOOD, PARKS]  # love + open, for the metrics
    assert json.loads(p.availability) == ["tue_evening", "sat_morning"]
    assert json.loads(p.skills) == ["admin", "media"]
    assert p.frequency == "monthly"
    html = user.get("/me/preferences/causes?saved=1").text
    assert "Saved." in html
    assert f'name="cause_{ANIMALS}" value="no" checked' in html


def test_a_cause_left_unrated_is_not_stored(user):
    form = {k: v for k, v in SAVE.items() if k != f"cause_{PARKS}"}
    user.post("/me/preferences", data=form)
    assert json.loads(profile_of(user).cause_prefs) == {FOOD: "love", ANIMALS: "no"}


@pytest.mark.parametrize(
    "bad",
    [
        {f"cause_{FOOD}": "adore"},
        {"availability": ["tuesday_brunch"]},
        {"skills": ["juggling"]},
        {"frequency": "hourly"},
    ],
)
def test_an_unknown_answer_saves_nothing(user, bad):
    before = answers(user)
    r = user.post("/me/preferences", data={**SAVE, **bad}, follow_redirects=False)
    assert r.status_code == 200 and "wasn't recognized" in r.text
    assert answers(user) == before


def test_at_least_one_cause_must_be_loved_or_open(user):
    form = {f"cause_{FOOD}": "no", f"cause_{PARKS}": "no"}
    r = user.post("/me/preferences", data=form, follow_redirects=False)
    assert r.status_code == 200 and "at least one cause" in r.text
    assert json.loads(profile_of(user).cause_prefs) == {FOOD: "love"}


def test_a_volunteer_with_only_inactive_causes_can_still_save_the_other_pages(
    homelessness_off, user
):
    (dead,) = [c for c in causes() if not c.active][:1]
    profiles.update(id=uid_of(user), cause_prefs=json.dumps({dead.slug: "love"}))
    r = user.post(
        "/me/preferences",
        data={"sections": "when", "back": "when", "availability": ["mon_morning"]},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("/when?saved=1")
    assert json.loads(profile_of(user).availability) == ["mon_morning"]
    r = user.post("/me/preferences", data={"cause_" + dead.slug: "love"})
    assert "at least one cause" in r.text  # the Causes page itself is still held to it


def test_a_rating_of_an_inactive_cause_is_kept(homelessness_off, user):
    (dead,) = [c for c in causes() if not c.active][:1]
    profiles.update(
        id=uid_of(user), cause_prefs=json.dumps({FOOD: "love", dead.slug: "open"})
    )
    user.post("/me/preferences", data=SAVE)
    assert json.loads(profile_of(user).cause_prefs)[dead.slug] == "open"


def test_a_coordinator_is_sent_to_their_dashboard(coord):
    r = coord.get("/me/preferences", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/org"


def test_preferences_stay_private(user, coord):
    """A nonprofit sees the causes a volunteer would help with, never the ratings."""
    user.post("/me/preferences", data=SAVE)
    opening = next(
        o for o in openings() if o.org_id == coord.org_id and o.status == "open"
    )
    data.swipe(uid_of(user), opening.id, True)
    inbox = coord.get("/org/inbox").text
    assert "Food insecurity" in inbox
    assert "Animal shelters" not in inbox and "Not for me" not in inbox


# --- the deck ------------------------------------------------------------------------------


def rate(client, **states):
    profiles.update(
        id=uid_of(client),
        cause_prefs=json.dumps({s.replace("_", "-"): v for s, v in states.items()}),
    )


def test_not_for_me_hides_the_cause_everywhere(bare):
    add("food")
    animals = add("animals", cause=ANIMALS)
    mixed = add("food and animals", cause=[FOOD, ANIMALS])
    rate(bare, food_insecurity="love", animal_shelters="no")
    assert deck_titles(bare) == ["food"]
    for oid in (animals, mixed):
        with pytest.raises(LookupError):
            data.swipe(uid_of(bare), oid, True)


def test_love_ranks_above_open_above_unrated(bare):
    # inserted worst first, at the same time and place: only the cause can order them
    add("unrated", cause=ANIMALS)
    add("open", cause=PARKS)
    add("love", cause=FOOD)
    rate(bare, food_insecurity="love", environment_parks="open")
    assert deck_titles(bare) == ["love", "open", "unrated"]


def test_open_alone_still_ranks_above_unrated(bare):
    add("unrated", cause=ANIMALS)
    add("open", cause=PARKS)
    rate(bare, environment_parks="open", food_insecurity="no")
    assert deck_titles(bare) == ["open", "unrated"]


def test_a_saved_not_for_me_reaches_the_deck_page(bare):
    add("animals only", cause=ANIMALS)
    assert "animals only" in bare.get("/deck").text
    bare.post("/me/preferences", data=SAVE)
    assert "animals only" not in bare.get("/deck").text


# --- cause_fit and the migration -----------------------------------------------------------


def test_cause_fit_with_loved_causes_only_is_jaccard():
    """So a migrated profile, whose causes all became "love", ranks exactly as before."""
    slugs = ["a", "b", "c", "d"]
    sets = [
        set(c) for n in range(len(slugs) + 1) for c in itertools.combinations(slugs, n)
    ]
    for mine, theirs in itertools.product(sets, sets):
        union = mine | theirs
        want = len(mine & theirs) / len(union) if union else 0
        assert match.cause_fit(dict.fromkeys(mine, "love"), theirs) == pytest.approx(
            want
        )


def test_cause_fit_values():
    mine = {"a": "love", "b": "open", "c": "no"}
    assert match.cause_fit(mine, {"a"}) == 1
    assert match.cause_fit(mine, {"b"}) == pytest.approx(0.6 / 2)
    assert match.cause_fit(mine, {"d"}) == 0
    assert match.cause_fit({"b": "open"}, {"b"}) == pytest.approx(0.6)
    assert match.cause_fit({}, set()) == 0


# The deck as app/data.py and app/match.py ranked it at d4906d4, before preferences,
# copied here as an oracle: jaccard causes, the radius as both gate and distance scale,
# weekday/weekend buckets, and its weights.
OLD_WEIGHTS = {"causes": 0.35, "skills": 0.20, "distance": 0.20, "availability": 0.15,
               "reply": 0.10}  # fmt: skip


def old_bucket(starts_at):
    t = datetime.fromisoformat(starts_at)
    part = "morning" if t.hour < 12 else "afternoon" if t.hour < 17 else "evening"
    return f"{'weekend' if t.weekday() >= 5 else 'weekday'}_{part}"


def old_titles(p, rows):
    """d4906d4's _deck_openings + deck() order for profile p, no swipes, no reply history."""
    active = {c.slug for c in causes() if c.active}
    mine = set(json.loads(p.causes)) & active
    ranked = []
    for o in rows:
        theirs = set(json.loads(o.causes)) & active
        if not theirs:
            continue
        km = None
        if not o.remote:
            if None in (o.lat, o.lon):
                continue
            km = match.haversine_km(p.lat, p.lon, o.lat, o.lon)
            if p.radius_km and p.radius_km < 60 and km > p.radius_km:
                continue
        union = mine | theirs
        wanted = set(json.loads(o.skills))
        fit = (
            OLD_WEIGHTS["causes"] * (len(mine & theirs) / len(union) if union else 0)
            + OLD_WEIGHTS["skills"]
            * (len(wanted & set(json.loads(p.skills))) / len(wanted) if wanted else 0.5)
            + OLD_WEIGHTS["distance"]
            * (1.0 if o.remote else max(0.0, 1 - km / p.radius_km))
            + OLD_WEIGHTS["availability"]
            * (
                1.0
                if old_bucket(o.starts_at) in json.loads(p.availability)
                else 0.5
                if o.recurrence == "flexible"
                else 0.0
            )
            + OLD_WEIGHTS["reply"] * 0.5
        )
        ranked.append((-round(fit, 6), o.starts_at, o.id, o.title))
    return [t for *_, t in sorted(ranked)]


def test_a_migrated_profile_keeps_its_deck_order(bare, monkeypatch):
    """A profile in the d4906d4 shape ranks exactly as d4906d4 did, before and after the
    0010/0011 migrations. The weights are pinned to d4906d4's (the deck was reweighted
    for everyone since, on purpose), so this checks every term and the gate, not the weights."""
    monkeypatch.setattr(match, "WEIGHTS", {**OLD_WEIGHTS, "work": 0, "language": 0})
    rng = random.Random(7)
    combos = [[FOOD], [PARKS], [ANIMALS], [FOOD, PARKS], [FOOD, ANIMALS]]
    for i in range(60):
        kind = rng.random()
        add(
            f"o{i}",
            cause=rng.choice(combos),
            km=rng.uniform(0.2, 26),  # some past the 16 km radius below
            starts_at=f"2099-01-{rng.randint(1, 28):02d}T{rng.randint(7, 21):02d}:00",
            skills=json.dumps(
                rng.sample(["car", "admin", "physical"], rng.randint(0, 2))
            ),
            recurrence=rng.choice(["one_time", "weekly", "flexible"]),
            remote=kind < 0.1,
            **({"lat": None, "lon": None} if kind < 0.05 else {}),
        )
    uid = uid_of(bare)
    profiles.update(
        id=uid,
        causes=json.dumps([FOOD, ANIMALS]),
        cause_prefs=None,
        availability='["weekend_morning", "weekday_evening"]',
        skills='["car"]',
        radius_km=16,
    )
    want = old_titles(profiles[uid], openings(where="status = 'open'"))
    assert 30 < len(want) < 60  # the gate hides some, remote ones are kept
    assert deck_titles(bare) == want  # read in the legacy shape
    with db.conn:
        cause_prefs_migration.up(db.conn)
        day_slots_migration.up(db.conn)
    p = profiles[uid]
    assert json.loads(p.cause_prefs) == {FOOD: "love", ANIMALS: "love"}
    assert "sat_morning" in json.loads(p.availability)
    assert deck_titles(bare) == want  # and migrated


def test_the_migration_adds_the_column_to_an_old_table(tmp_path):
    conn = apsw.Connection(str(tmp_path / "old.db"))
    conn.execute("CREATE TABLE profile (id INTEGER PRIMARY KEY, causes TEXT)")
    conn.execute(
        "INSERT INTO profile VALUES (1, '[\"food-insecurity\"]'), (2, NULL), (3, '[]')"
    )
    cause_prefs_migration.up(conn)
    cause_prefs_migration.up(conn)  # a second run changes nothing
    rows = conn.execute("SELECT id, cause_prefs FROM profile ORDER BY id").fetchall()
    assert rows == [(1, '{"food-insecurity": "love"}'), (2, None), (3, "{}")]


def test_homelessness_services_is_offered_now_that_it_is_active(user):
    html = user.get("/me/preferences/causes").text
    assert 'name="cause_homelessness-services" value="love"' in html


def test_the_page_marks_exactly_what_a_nonprofit_sees(user):
    """Causes, general availability, languages and how often are shared after a like
    (choices.SHARED_WITH_NONPROFIT); places, ratings, dates, kind of work, skills and the
    Only-show choices are not. Each section page says so once, under its title, and the
    last-minute page says nothing: it must not promise everything is private."""
    from html import unescape

    from app.choices import SHARED_WITH_NONPROFIT
    from app.routes.prefs import SHARED

    assert 'href="/privacy"' in user.get("/me/preferences").text
    counts = {
        k: unescape(user.get(f"/me/preferences/{k}").text).count(SHARED)
        for k in ("causes", "when", "work", "urgent")
    }
    assert counts == {"causes": 1, "when": 1, "work": 1, "urgent": 0}
    work = unescape(user.get("/me/preferences/work").text)
    assert work.index(SHARED) < work.index("<h2")  # a legend, not a card's own note
    for item in ("causes", "availability", "languages", "how often"):
        assert item in SHARED_WITH_NONPROFIT


def test_each_only_show_toggle_saves_from_the_section_it_governs(user):
    """Prefer / Only show sits under its own controls; surprise and verified hours save
    from the causes page, and a page that doesn't carry them leaves them alone."""
    pages = {
        "causes": ("only_causes", "only_distance"),
        "when": ("only_when",),
        "work": ("only_work",),
    }
    for key, names in pages.items():
        html = user.get(f"/me/preferences/{key}").text
        assert all(f'name="{n}" value="only"' in html for n in names), key
    assert 'name="only_work"' not in user.get("/me/preferences/when").text
    r = user.post(
        "/me/preferences",
        data={
            **SAVE,
            "sections": "causes",
            "back": "causes",
            "only_causes": "only",
            "only_distance": "prefer",
            "surprise": "1",
            "verified_hours": "prefer",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    saved = data.preferences(uid_of(user))
    assert saved["dealbreakers"] == ["causes"]
    assert saved["surprise"] and saved["verified_hours"] == "prefer"
    for key, field in (("when", "only_when"), ("work", "only_work")):
        user.post(
            "/me/preferences",
            data={"sections": key, "back": key, field: "only"},
        )
    saved = data.preferences(uid_of(user))
    assert saved["dealbreakers"] == ["causes", "when", "work"]
    assert saved["surprise"] and saved["verified_hours"] == "prefer"  # kept
