"""profile.name_changed_at: when a volunteer last changed their first or last name (UTC).

A certificate lists only hours confirmed after that moment, so one person's hours cannot
be certified under several names (data.issue_certificate). Runs before app/db.py declares
the tables, so it adds the column itself; NULL means the name was never changed.
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(profile)")}
    if cols and "name_changed_at" not in cols:
        conn.execute("ALTER TABLE profile ADD COLUMN name_changed_at TEXT")
