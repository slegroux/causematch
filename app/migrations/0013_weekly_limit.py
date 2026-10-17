"""A weekly limit: "Up to N shifts a week" (app/prefs.py weekly_limit), NULL for off.

Runs before app/db.py declares the tables, so it adds the column itself.
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(profile)")}
    if cols and "weekly_limit" not in cols:
        conn.execute("ALTER TABLE profile ADD COLUMN weekly_limit INTEGER")
