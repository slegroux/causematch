"""The columns `transform=True` added from the classes, with no migration of their own.

Between the baseline (0001) and the end of transform, these columns reached an existing
database only through app/db.py's `db.create(..., transform=True)`. app/db.py no longer
reshapes tables, and stops when a table lacks a column its class declares, so a database
last started before one of them was added gets it here. Runs before app/db.py declares the
tables, so it adds the columns itself; existing rows keep NULL, as transform left them.
"""

COLUMNS = (
    ("org", "logo_key", "TEXT"),
    ("org", "photo_key", "TEXT"),
    ("org", "website", "TEXT"),
    ("opening", "setting", "TEXT"),
    ("opening", "effort", "TEXT"),
    ("opening", "group_size", "TEXT"),
    ("opening", "first_timer_ok", "INTEGER"),
    ("opening", "languages", "TEXT"),
    ("opening", "photo_key", "TEXT"),
    ("opening", "event_url", "TEXT"),
    ("profile", "free_dates", "TEXT"),
    ("profile", "horizon_days", "INTEGER"),
    ("profile", "commitment", "TEXT"),
    ("profile", "setting", "TEXT"),
    ("profile", "effort", "TEXT"),
    ("profile", "group_size", "TEXT"),
    ("profile", "first_timer", "INTEGER"),
    ("profile", "orientation_ok", "INTEGER"),
    ("profile", "languages", "TEXT"),
    ("profile", "dealbreakers", "TEXT"),
    ("profile", "surprise", "INTEGER"),
    ("profile", "photo_key", "TEXT"),
    ("profile", "last_name", "TEXT"),
    ("profile", "pronouns", "TEXT"),
    ("profile", "bio", "TEXT"),
    ("profile", "interests", "TEXT"),
    ("profile", "photo_nudge_dismissed_at", "TEXT"),
    ("profile", "matches_seen_at", "TEXT"),
    ("profile", "match_moment_seen_at", "TEXT"),
    ("profile", "urgent_on", "INTEGER"),
    ("profile", "urgent_window", "TEXT"),
    ("profile", "urgent_quiet_from", "INTEGER"),
    ("profile", "urgent_quiet_to", "INTEGER"),
    ("profile", "urgent_per_week", "INTEGER"),
    ("login_token", "email", "TEXT"),
    ("match", "urgent_call_id", "INTEGER"),
)


def up(conn):
    for table, column, kind in COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE [{table}] ADD COLUMN [{column}] {kind}")
