"""A volunteer's match preferences, read the same way whatever shape the profile is in.

Private: nonprofits never see them. A profile from before a preference existed reads as
the answers it already had, so its deck stays as it was until the volunteer edits it.
"""

import json
import re
from datetime import date, timedelta

from app.choices import DEFAULT_DEALBREAKERS, MAX_FREE_DATES, MAX_MINUTES, MODES
from app.forms import HIDDEN_CHARS_MESSAGE, hidden_chars
from app.geo import neighborhood_latlon, round_point, zip_latlon

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
UNRECOGNIZED = "Something in that form wasn't recognized. Please try again."
LOVE, OPEN, NO = "love", "open", "no"
CAUSE_STATES = [(LOVE, "Love"), (OPEN, "Open to it"), (NO, "Not for me")]


def cause_prefs(profile):
    """{slug: love|open|no}; a cause left out is unrated. Before cause_prefs existed, the
    picked causes were all loved."""
    if profile.cause_prefs is None:
        return {s: LOVE for s in json.loads(profile.causes or "[]")}
    states = {s for s, _ in CAUSE_STATES}
    return {s: v for s, v in json.loads(profile.cause_prefs).items() if v in states}


def free_dates(profile, today):
    """The dates the volunteer said they're free, from today on ("YYYY-MM-DD")."""
    return sorted(d for d in json.loads(profile.free_dates or "[]") if d >= today)


FREE_DATES_AHEAD = 366  # days: a free date further out than a year is a typo


def parse_free_dates(kept, new, today):
    """(sorted unique dates, error or None) from the form: the dates kept as chips and the
    one typed in. Kept dates that have passed since the page loaded drop silently; a new one
    must be from today on and within FREE_DATES_AHEAD days. At most MAX_FREE_DATES."""
    last = (date.fromisoformat(today) + timedelta(days=FREE_DATES_AHEAD)).isoformat()
    if not all(ISO_DATE.fullmatch(d) for d in [*kept, *([new] if new else [])]):
        return [], UNRECOGNIZED
    try:
        for d in [*kept, new or today]:
            date.fromisoformat(d)
    except ValueError:
        return [], UNRECOGNIZED
    if new and new < today:
        return [], "Pick a date from today on."
    if new and new > last:
        return [], "Pick a date within the next year."
    dates = {d for d in kept if today <= d <= last} | ({new} if new else set())
    if len(dates) > MAX_FREE_DATES:
        return [], f"Up to {MAX_FREE_DATES} dates: untick one to add another."
    return sorted(dates), None


def horizon_days(profile):
    """How far ahead to show shifts, in days; 0 for any time."""
    return profile.horizon_days or 0


def commitment(profile):
    return profile.commitment or "either"


def dealbreakers(profile):
    """The preferences that filter the deck (choices.DEALBREAKERS); the rest only rank."""
    if profile.dealbreakers is None:
        return list(DEFAULT_DEALBREAKERS)
    return json.loads(profile.dealbreakers)


def work(profile):
    """The kind of work a volunteer prefers, as match.work_fit takes it."""
    return {
        "setting": profile.setting or "either",
        "effort": profile.effort or "any",
        "group_size": profile.group_size or "any",
        "first_timer": bool(profile.first_timer),
    }


def orientation_ok(profile):
    """False: openings with a separate orientation session are hidden."""
    return profile.orientation_ok is None or bool(profile.orientation_ok)


def languages(profile):
    return json.loads(profile.languages or "[]")


def picked(prefs):
    """The causes a volunteer would help with (love, then open), for the `causes` column."""
    return [s for s, v in prefs.items() if v == LOVE] + [
        s for s, v in prefs.items() if v == OPEN
    ]


MY_LOCATION = "My location"
LABEL_MAX = 20
DEFAULT_LABELS = ("Home", "Work")


def place_form(place=None, slot=0):
    """A place as its form fields (all text). Never the coordinates: a saved place keeps its
    point while its area text is unchanged."""
    if place is None:
        return {
            "label": DEFAULT_LABELS[slot],
            "where": "",
            "lat": "",
            "lon": "",
            "mode": "car",
            "minutes": "30",
        }
    return {
        "label": place["label"],
        "where": place["area"],
        "lat": "",
        "lon": "",
        "mode": place["mode"],
        "minutes": str(place["max_minutes"]),
    }


def resolve_place(f, current, slot):
    """(place or None, error or None) from one place's form fields.

    The point comes from, in order: a device location (rounded to 2 decimals, again: the
    browser already rounded it), the saved place when its area text is unchanged, a ZIP,
    a neighborhood. An empty "where" is no place.
    """
    if hidden_chars(f["label"]) or hidden_chars(f["where"]):
        return None, HIDDEN_CHARS_MESSAGE
    label = " ".join(f["label"].split()) or DEFAULT_LABELS[slot]
    where = " ".join(f["where"].split())
    if not where:
        return None, None
    if len(label) > LABEL_MAX:
        return None, f"A place's name can be up to {LABEL_MAX} characters."
    try:
        max_minutes = int(f["minutes"])
    except ValueError:
        max_minutes = -1
    if f["mode"] not in {m for m, _ in MODES} or max_minutes not in {
        m for m, _ in MAX_MINUTES
    }:
        return None, UNRECOGNIZED
    point, area = None, where
    if where == MY_LOCATION and (f["lat"] or f["lon"]):
        point = round_point(f["lat"], f["lon"])
        if point is None:
            return None, "That location isn't in Los Angeles County."
    elif current and where == current["area"]:
        point = current["lat"], current["lon"]
    elif where.isdigit() and len(where) == 5:
        point = zip_latlon(where)
    elif found := neighborhood_latlon(where):
        area, point = found
    if point is None:
        return None, (
            f"We couldn't find “{where[:80]}” in Los Angeles County. Pick a neighborhood "
            "from the list, or type a ZIP code."
        )
    return {
        "label": label,
        "lat": point[0],
        "lon": point[1],
        "area": area,
        "mode": f["mode"],
        "max_minutes": max_minutes,
    }, None
