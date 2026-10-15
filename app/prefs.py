"""A volunteer's match preferences, read the same way whatever shape the profile is in.

Private: nonprofits never see them. A profile from before a preference existed reads as
the answers it already had, so its deck stays as it was until the volunteer edits it.
"""

import json

LOVE, OPEN, NO = "love", "open", "no"
CAUSE_STATES = [(LOVE, "Love"), (OPEN, "Open to it"), (NO, "Not for me")]


def cause_prefs(profile):
    """{slug: love|open|no}; a cause left out is unrated. Before cause_prefs existed, the
    picked causes were all loved."""
    if profile.cause_prefs is None:
        return {s: LOVE for s in json.loads(profile.causes or "[]")}
    states = {s for s, _ in CAUSE_STATES}
    return {s: v for s, v in json.loads(profile.cause_prefs).items() if v in states}


def picked(prefs):
    """The causes a volunteer would help with (love, then open), for the `causes` column."""
    return [s for s, v in prefs.items() if v == LOVE] + [
        s for s, v in prefs.items() if v == OPEN
    ]
