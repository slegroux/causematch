"""Three-state causes: each volunteer's picked causes become "love" in cause_prefs.

`causes` stays as it was (love + open from now on), for the metrics and the nonprofit's
view. Runs before app/db.py declares the tables, so it adds the column itself.
"""

import json


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(profile)")}
    if not cols:
        return
    if "cause_prefs" not in cols:
        conn.execute("ALTER TABLE profile ADD COLUMN cause_prefs TEXT")
    rows = conn.execute(
        "SELECT id, causes FROM profile WHERE cause_prefs IS NULL AND causes IS NOT NULL"
    ).fetchall()
    for pid, causes in rows:
        conn.execute(
            "UPDATE profile SET cause_prefs = ? WHERE id = ?",
            [json.dumps({s: "love" for s in json.loads(causes)}), pid],
        )
