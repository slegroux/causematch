"""Ranking for the volunteer deck. Pure functions: data.py feeds them plain values."""

from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

from app.choices import DAYS, PARTS

LA = ZoneInfo("America/Los_Angeles")
EARTH_KM = 6371.0088
WEIGHTS = {
    "causes": 0.30,
    "distance": 0.18,
    "availability": 0.15,
    "skills": 0.12,
    "work": 0.10,
    "reply": 0.10,
    "language": 0.05,
}


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * EARTH_KM * asin(sqrt(a))


# Door-to-door averages for Los Angeles, stops and parking included; tunable. No routing
# API: travel minutes are estimated from the straight-line distance.
MODE_KMH = {"car": 32, "transit": 14, "bike": 13, "walk": 4.5}


def minutes(km, mode):
    """Estimated travel minutes for a straight-line distance; at least 1."""
    return max(1, round(km / MODE_KMH[mode] * 60))


def reach_km(mode, max_minutes):
    """How far max_minutes goes in this mode; None for any distance (0)."""
    return max_minutes * MODE_KMH[mode] / 60 if max_minutes else None


def best_place(places, lat, lon):
    """(place, km, minutes) from the place quickest to reach this point from, among the
    places whose reach covers it; None when none does.

    A place is a dict with lat, lon, mode and reach_km (None: any distance).
    """
    best = None
    for p in places:
        km = haversine_km(p["lat"], p["lon"], lat, lon)
        if p["reach_km"] is not None and km > p["reach_km"]:
            continue
        m = minutes(km, p["mode"])
        if best is None or m < best[2]:
            best = (p, km, m)
    return best


def jaccard(a, b):
    """Overlap of two collections as |a & b| / |a | b|; 0 when both are empty."""
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


CAUSE_VALUE = {"love": 1.0, "open": 0.6}  # anything else (unrated) counts 0


def cause_fit(mine, theirs):
    """How well an opening's causes suit a volunteer, in 0..1.

    mine is {slug: love|open|no}, or a collection of slugs, all loved. Each of the opening's
    causes counts CAUSE_VALUE, over the union of its causes and the loved ones. With loved
    causes only this is jaccard, so a profile from before three-state causes ranks as it did.
    """
    if not isinstance(mine, dict):
        mine = dict.fromkeys(mine, "love")
    theirs = set(theirs)
    union = theirs | {s for s, v in mine.items() if v == "love"}
    if not union:
        return 0.0
    return sum(CAUSE_VALUE.get(mine.get(s), 0.0) for s in theirs) / len(union)


def causes_allowed(mine, theirs):
    """False when any of the opening's causes is "not for me": those are never shown."""
    return not any(mine.get(s) == "no" for s in theirs)


DAY_KEYS = [d for d, _ in DAYS]
# The older weekday/weekend buckets, as the day slots each one stands for
BUCKET_DAYS = {"weekday": DAY_KEYS[:5], "weekend": DAY_KEYS[5:]}


def _local(starts_at):
    t = datetime.fromisoformat(starts_at) if isinstance(starts_at, str) else starts_at
    return t.astimezone(LA) if t.tzinfo else t


def availability_slot(starts_at):
    """'sat_morning' etc. for a Los Angeles wall-clock start (str or datetime)."""
    t = _local(starts_at)
    part = "morning" if t.hour < 12 else "afternoon" if t.hour < 17 else "evening"
    return f"{DAY_KEYS[t.weekday()]}_{part}"


def availability_bucket(starts_at):
    """'weekday_morning' etc.: the older, coarser summary of availability_slot. Kept for
    compatibility (tests/test_match_unit.py); the app ranks by availability_slot."""
    day, part = availability_slot(starts_at).split("_")
    return f"{'weekend' if day in BUCKET_DAYS['weekend'] else 'weekday'}_{part}"


def day_slots(values):
    """Availability as day slots, in grid order: a slot stays, an older bucket
    ('weekday_morning') becomes the slots it stands for (Mon-Fri mornings), the rest drops."""
    want = set()
    for v in values:
        kind, _, part = v.partition("_")
        want.update(f"{d}_{part}" for d in BUCKET_DAYS.get(kind, [kind]))
    return [f"{d}_{p}" for d in DAY_KEYS for p, _ in PARTS if f"{d}_{p}" in want]


def general_buckets(values):
    """The weekday/weekend buckets any of these slots falls in: what a nonprofit sees."""
    slots = set(day_slots(values))
    return [
        f"{kind}_{p}"
        for kind, days in BUCKET_DAYS.items()
        for p, _ in PARTS
        if any(f"{d}_{p}" in slots for d in days)
    ]


def availability_overlap(
    volunteer_availability, starts_at, recurrence="", free_dates=()
):
    """1.0 when free then (that day and part of the week, or a date they said they're free);
    otherwise 0.5 for a flexible opening, else 0.0.

    Weekly and monthly openings repeat in the same slot, so they get no partial credit.
    """
    if availability_slot(starts_at) in day_slots(volunteer_availability):
        return 1.0
    if _local(starts_at).date().isoformat() in set(free_dates):
        return 1.0
    return 0.5 if recurrence == "flexible" else 0.0


def commitment_fits(commitment, recurrence):
    """False when the volunteer wants one-time help and the opening repeats, or the other
    way round. A flexible or unsaid opening fits either."""
    repeats = recurrence in ("weekly", "monthly")
    once = recurrence == "one_time"
    return not (
        commitment == "one_time" and repeats or commitment == "recurring" and once
    )


def skill_coverage(volunteer_skills, opening_skills):
    """Share of the skills an opening asks for that the volunteer has; 0.5 when it asks none.

    Coverage, not overlap: listing more skills can never raise an opening's score for a
    volunteer who lacks them, and an opening that needs nothing is neutral.
    """
    wanted = set(opening_skills)
    if not wanted:
        return 0.5
    return len(wanted & set(volunteer_skills)) / len(wanted)


def reply_rate(decided_within_48h, eligible):
    """Share of an org's eligible matches decided within 48 h; 0.5 with no history."""
    return decided_within_48h / eligible if eligible else 0.5


def distance_score(distance_km, radius_km, remote=False):
    """1 for remote, falling linearly to 0 at the volunteer's radius. Unknown counts as 0."""
    if remote:
        return 1.0
    if distance_km is None or not radius_km:
        return 0.0
    return max(0.0, 1 - distance_km / radius_km)


WORK_KEYS = ("setting", "effort", "group_size")
NO_PREFERENCE = {None, "", "either", "any"}


def work_fit(mine, theirs):
    """Kind of work in 0..1: the mean over setting, effort, group size and first-timer
    friendliness. Each is 1 when they agree, 0 when they differ, and 0.5 when the opening
    doesn't say or the volunteer doesn't mind, so an opening never loses by saying nothing.

    mine: {setting, effort, group_size, first_timer}; theirs: the same with first_timer_ok.
    """
    terms = []
    for k in WORK_KEYS:
        want, have = mine.get(k), theirs.get(k)
        terms.append(0.5 if want in NO_PREFERENCE or not have else float(want == have))
    terms.append(
        1.0 if mine.get("first_timer") and theirs.get("first_timer_ok") else 0.5
    )
    return sum(terms) / len(terms)


def work_allowed(mine, theirs):
    """The "work" dealbreaker: False when the opening says a setting, effort or group size
    other than the one the volunteer asked for. Saying nothing never hides an opening."""
    return not any(
        mine.get(k) not in NO_PREFERENCE and theirs.get(k) and theirs[k] != mine[k]
        for k in WORK_KEYS
    )


def work_matches(mine, theirs):
    """The kinds of work where the opening says what the volunteer asked for."""
    return [
        k
        for k in WORK_KEYS
        if mine.get(k) not in NO_PREFERENCE and mine.get(k) == theirs.get(k)
    ]


def language_fit(mine, theirs):
    """1 when the volunteer speaks a language the opening says helps, else 0.5: a language
    helps, it is never required."""
    return 1.0 if set(mine) & set(theirs) else 0.5


def score(
    *,
    causes,
    skills,
    distance,
    availability,
    reply,
    work=0.5,
    language=0.5,
):
    """Weighted fit in 0..1; each argument is already a 0..1 term, except causes
    (compared by cause_fit) and skills (by coverage), which are (volunteer, opening) pairs.
    work and language default to neutral."""
    return (
        WEIGHTS["causes"] * cause_fit(*causes)
        + WEIGHTS["skills"] * skill_coverage(*skills)
        + WEIGHTS["distance"] * distance
        + WEIGHTS["availability"] * availability
        + WEIGHTS["reply"] * reply
        + WEIGHTS["work"] * work
        + WEIGHTS["language"] * language
    )
