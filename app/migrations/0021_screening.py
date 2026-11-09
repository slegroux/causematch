"""opening.screening_required and opening.screening_kind: the nonprofit runs its own
screening (choices.SCREENING_KINDS) and confirms each volunteer before a first shift.

Runs before app/db.py declares the tables, so it adds the columns itself. No SQL default
(a new database built from the class has none, and the columns must match); existing
openings are set to 0, not required, and their kind stays NULL.
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(opening)")}
    if not cols:
        return
    if "screening_required" not in cols:
        conn.execute("ALTER TABLE opening ADD COLUMN screening_required INTEGER")
        conn.execute("UPDATE opening SET screening_required = 0")
    if "screening_kind" not in cols:
        conn.execute("ALTER TABLE opening ADD COLUMN screening_kind TEXT")
