"""Ranking for the volunteer deck. Pure functions: data.py feeds them plain values."""

from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
EARTH_KM = 6371.0088
WEIGHTS = {
    "causes": 0.35,
    "skills": 0.20,
    "distance": 0.20,
    "availability": 0.15,
    "reply": 0.10,
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


def availability_bucket(starts_at):
    """'weekday_morning' etc. for a Los Angeles wall-clock start (str or datetime)."""
    t = datetime.fromisoformat(starts_at) if isinstance(starts_at, str) else starts_at
    if t.tzinfo:
        t = t.astimezone(LA)
    part = "morning" if t.hour < 12 else "afternoon" if t.hour < 17 else "evening"
    return f"{'weekend' if t.weekday() >= 5 else 'weekday'}_{part}"


def availability_overlap(volunteer_availability, starts_at, recurrence=""):
    """1.0 when free then; otherwise 0.5 for a flexible opening, else 0.0.

    Weekly and monthly openings repeat in the same bucket, so they get no partial credit.
    """
    if availability_bucket(starts_at) in set(volunteer_availability):
        return 1.0
    return 0.5 if recurrence == "flexible" else 0.0


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


def score(
    *,
    causes,
    skills,
    distance,
    availability,
    reply,
):
    """Weighted fit in 0..1; each argument is already a 0..1 term, except causes
    (compared by cause_fit) and skills (by coverage), which are (volunteer, opening) pairs."""
    return (
        WEIGHTS["causes"] * cause_fit(*causes)
        + WEIGHTS["skills"] * skill_coverage(*skills)
        + WEIGHTS["distance"] * distance
        + WEIGHTS["availability"] * availability
        + WEIGHTS["reply"] * reply
    )
