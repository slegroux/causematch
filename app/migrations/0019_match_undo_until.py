"""match.undo_until: until when a coordinator's Decline can be taken back (UTC).

Until then the volunteer is shown nothing of the decline (data.decide, undo_decline).
Runs before app/db.py declares the tables, so it adds the column itself; NULL means the
decision was never undoable.
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(match)")}
    if cols and "undo_until" not in cols:
        conn.execute("ALTER TABLE match ADD COLUMN undo_until TEXT")
