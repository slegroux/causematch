"""match.screening_status and the screening table: a like accepted on an opening that
needs the nonprofit's own screening holds its first shift until a coordinator confirms
(data.decide, data.confirm_screening).

Runs before app/db.py declares the tables, so it adds the column and creates the table
itself (the same columns as db.Screening) with the index decide's lookup reads. No SQL
default (a new database built from the classes has none, and the columns must match):
existing matches keep NULL, which means no screening was ever asked of them, as their
openings required none before 0022.
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(match)")}
    if cols and "screening_status" not in cols:
        conn.execute("ALTER TABLE match ADD COLUMN screening_status TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS screening (id INTEGER PRIMARY KEY, "
        "volunteer_id INTEGER, org_id INTEGER, kind TEXT, cleared_at TEXT, "
        "cleared_by INTEGER, created_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_screening_volunteer_id_org_id "
        "ON screening (volunteer_id, org_id)"
    )
