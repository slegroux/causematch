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


def jaccard(a, b):
    """Overlap of two collections as |a & b| / |a | b|; 0 when both are empty."""
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


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
    (compared by jaccard) and skills (by coverage), which are (volunteer, opening) pairs."""
    return (
        WEIGHTS["causes"] * jaccard(*causes)
        + WEIGHTS["skills"] * skill_coverage(*skills)
        + WEIGHTS["distance"] * distance
        + WEIGHTS["availability"] * availability
        + WEIGHTS["reply"] * reply
    )
