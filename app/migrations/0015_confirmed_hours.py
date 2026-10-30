"""confirmed_minutes on shift: the hours a coordinator confirmed when marking it attended.

Runs before app/db.py declares the tables, so it adds the column itself; shifts marked
before this keep NULL (attended, hours unknown).
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shift)")}
    if cols and "confirmed_minutes" not in cols:
        conn.execute("ALTER TABLE shift ADD COLUMN confirmed_minutes INTEGER")
