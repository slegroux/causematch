"""Shift.confirmed_at: stamped when the volunteer answers "Still coming?" with yes, from
the day-before reminder's link. Runs before app/db.py declares the tables, so it adds the
column itself; existing shifts stay unconfirmed (NULL)."""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shift)")}
    if cols and "confirmed_at" not in cols:
        conn.execute("ALTER TABLE shift ADD COLUMN confirmed_at TEXT")
