"""Verified hours: org.confirms_hours (the nonprofit says it can confirm volunteer hours)
and profile.verified_hours (a volunteer's private wish: prefer|only; NULL: no preference).

Runs before app/db.py declares the tables, so it adds the columns itself; every org
starts as not confirming and every volunteer with no preference.
"""


def up(conn):
    for table, column, kind in (
        ("org", "confirms_hours", "INTEGER"),
        ("profile", "verified_hours", "TEXT"),
    ):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
