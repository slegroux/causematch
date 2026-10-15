"""Availability by day: the weekday/weekend buckets become the day slots they stood for
(weekday_morning → mon_morning ... fri_morning; weekend_evening → sat_evening, sun_evening).

The mapping is written out here, not imported, so this step never changes after it ships.
Onboarding still asks the buckets until its rebuild; app/match.py day_slots reads both.
"""

import json

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PARTS = ("morning", "afternoon", "evening")
BUCKET_DAYS = {"weekday": DAYS[:5], "weekend": DAYS[5:]}


def slots(values):
    want = set()
    for v in values:
        kind, _, part = v.partition("_")
        want.update(f"{d}_{part}" for d in BUCKET_DAYS.get(kind, [kind]))
    return [f"{d}_{p}" for d in DAYS for p in PARTS if f"{d}_{p}" in want]


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(profile)")}
    if "availability" not in cols:
        return
    rows = conn.execute(
        "SELECT id, availability FROM profile WHERE availability IS NOT NULL"
    ).fetchall()
    for pid, raw in rows:
        try:
            values = json.loads(raw)
        except ValueError:
            continue  # a malformed row keeps reading as it did; it must not stop startup
        if not isinstance(values, list):
            continue
        new = json.dumps(slots(values))
        if new != raw:
            conn.execute("UPDATE profile SET availability = ? WHERE id = ?", [new, pid])
