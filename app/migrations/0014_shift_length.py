"""duration_minutes on opening and shift: how long a shift lasts, so hours can be counted.

Runs before app/db.py declares the tables, so it adds the columns itself; rows made before
this keep NULL (no length known).
"""


def up(conn):
    for table in ("opening", "shift"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "duration_minutes" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN duration_minutes INTEGER")
