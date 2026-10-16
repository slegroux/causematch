from datetime import UTC, datetime

import pytest

from app import match

SANTA_MONICA_PIER = (34.0092, -118.4973)
UNION_STATION = (34.0560, -118.2365)


def test_haversine_known_la_pair():
    km = match.haversine_km(*SANTA_MONICA_PIER, *UNION_STATION)
    assert km == pytest.approx(24, abs=1)


def test_haversine_zero_and_symmetric():
    assert match.haversine_km(*UNION_STATION, *UNION_STATION) == 0
    a = match.haversine_km(*SANTA_MONICA_PIER, *UNION_STATION)
    assert a == pytest.approx(match.haversine_km(*UNION_STATION, *SANTA_MONICA_PIER))


@pytest.mark.parametrize(
    "a, b, want",
    [
        ([], [], 0),
        (["x"], [], 0),
        ([], ["x"], 0),
        (["x"], ["x"], 1),
        (["x", "y"], ["y", "z"], 1 / 3),
        (["x", "x"], ["x"], 1),
        (("x", "y"), {"y"}, 0.5),
        (["x"], ["y"], 0),
    ],
)
def test_cause_fit_of_loved_causes_is_their_overlap(a, b, want):
    assert match.cause_fit(dict.fromkeys(a, "love"), b) == pytest.approx(want)


# 2026-10-17 is a Saturday, 2026-10-19 a Monday.
@pytest.mark.parametrize(
    "starts_at, slot",
    [
        ("2026-10-19T00:00", "mon_morning"),
        ("2026-10-19T11:59", "mon_morning"),
        ("2026-10-19T12:00", "mon_afternoon"),
        ("2026-10-19T16:59", "mon_afternoon"),
        ("2026-10-19T17:00", "mon_evening"),
        ("2026-10-23T23:59", "fri_evening"),  # Friday night is still Friday
        ("2026-10-24T00:00", "sat_morning"),  # Saturday
        ("2026-10-24T14:00", "sat_afternoon"),
        ("2026-10-25T20:00", "sun_evening"),  # Sunday
        ("2026-10-26T00:00", "mon_morning"),  # Monday
        ("2026-03-08T09:00", "sun_morning"),  # the day DST starts
        ("2026-11-01T09:00", "sun_morning"),  # the day DST ends
    ],
)
def test_slot_of_a_local_wall_clock_start(starts_at, slot):
    assert match.availability_slot(starts_at) == slot


@pytest.mark.parametrize(
    "utc, slot",
    [
        # 00:30 PDT Monday; a fixed UTC-8 would call it 23:30 Sunday
        (datetime(2026, 3, 9, 7, 30, tzinfo=UTC), "mon_morning"),
        # 23:30 PST Sunday; a fixed UTC-7 would call it 00:30 Monday
        (datetime(2026, 11, 2, 7, 30, tzinfo=UTC), "sun_evening"),
    ],
)
def test_aware_datetimes_are_read_in_los_angeles(utc, slot):
    assert match.availability_slot(utc) == slot


def test_availability_overlap_free_then():
    assert match.availability_overlap(["weekend_morning"], "2026-10-24T09:00") == 1.0


def test_availability_overlap_not_free_one_time():
    assert match.availability_overlap(["weekday_evening"], "2026-10-24T09:00") == 0.0
    assert match.availability_overlap([], "2026-10-24T09:00", "one_time") == 0.0


def test_availability_overlap_flexible_is_half():
    assert match.availability_overlap([], "2026-10-24T09:00", "flexible") == 0.5


@pytest.mark.parametrize("recurrence", ["weekly", "monthly", "one_time", ""])
def test_availability_overlap_repeating_gets_no_partial_credit(recurrence):
    # a weekly Saturday 09:00 is Saturday 09:00 every week
    assert match.availability_overlap([], "2026-10-24T09:00", recurrence) == 0.0


def test_availability_overlap_match_beats_recurring_half():
    free = ["weekend_morning"]
    assert match.availability_overlap(free, "2026-10-24T09:00", "weekly") == 1.0


def test_reply_rate_new_org_is_not_punished():
    assert match.reply_rate(0, 0) == 0.5


def test_reply_rate_share_of_eligible():
    assert match.reply_rate(3, 4) == 0.75
    assert match.reply_rate(0, 5) == 0
    assert match.reply_rate(5, 5) == 1


def test_distance_score():
    assert match.distance_score(0, 10) == 1
    assert match.distance_score(5, 10) == 0.5
    assert match.distance_score(10, 10) == 0
    assert match.distance_score(25, 10) == 0  # never negative
    assert match.distance_score(None, 10, remote=True) == 1
    assert match.distance_score(500, 10, remote=True) == 1
    assert match.distance_score(None, 10) == 0


def test_score_weights_sum_to_one_for_a_perfect_fit():
    perfect = {
        "causes": ({"a": "love"}, ["a"]),
        "skills": (["s"], ["s"]),
        "distance": 1,
        "availability": 1,
        "reply": 1,
        "work": 1,
        "language": 1,
    }
    assert match.score(**perfect) == pytest.approx(1)


@pytest.mark.parametrize(
    "term, weight",
    [
        ("causes", 0.30),
        ("distance", 0.18),
        ("availability", 0.15),
        ("skills", 0.12),
        ("work", 0.10),
        ("reply", 0.10),
        ("language", 0.05),
    ],
)
def test_each_term_carries_its_weight(term, weight):
    zero = {
        "causes": ({"a": "love"}, ["b"]),
        "skills": (["a"], ["b"]),
        "distance": 0,
        "availability": 0,
        "reply": 0,
        "work": 0,
        "language": 0,
    }
    full = {"causes": ({"a": "love"}, ["a"]), "skills": (["a"], ["a"])}
    one = {**zero, term: full.get(term, 1)}
    assert match.score(**zero) == 0
    assert match.score(**one) == pytest.approx(weight)


def test_skill_coverage_is_share_of_what_the_opening_asks():
    assert match.skill_coverage(["car"], ["car"]) == 1
    assert match.skill_coverage(["car", "admin"], ["car", "physical"]) == 0.5
    assert match.skill_coverage(["admin"], ["car", "physical"]) == 0
    assert match.skill_coverage([], ["car"]) == 0
    assert (
        match.skill_coverage(["car", "admin", "media"], ["car"]) == 1
    )  # extra skills are free


def test_skill_coverage_of_an_opening_asking_nothing_is_neutral():
    assert match.skill_coverage([], []) == 0.5
    assert match.skill_coverage(["car"], []) == 0.5


def test_listing_more_skills_never_beats_honesty():
    volunteer = ["car"]
    honest = match.skill_coverage(volunteer, ["car"])
    stuffed = match.skill_coverage(volunteer, ["car", "physical", "admin"])
    nothing = match.skill_coverage(volunteer, [])
    assert stuffed < honest and stuffed < nothing
    # and for a volunteer with none of them, stuffing is no better than asking for one
    assert match.skill_coverage(
        [], ["car", "physical", "admin"]
    ) == match.skill_coverage([], ["car"])
