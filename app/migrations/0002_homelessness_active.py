"""Homelessness services turns on (2026-10-15): group shifts only, which the opening form's
adults-only, no one-to-one confirmation already enforces. app/db.py's CAUSES has it active,
for new databases. It turns the cause on even if an admin had switched it off by hand: the
launch decision wins."""


def up(conn):
    conn.execute("UPDATE cause SET active = 1 WHERE slug = 'homelessness-services'")
