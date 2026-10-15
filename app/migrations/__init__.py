"""Numbered schema migrations, for the changes `db.create(..., transform=True)` can't make.

`transform=True` (app/db.py) is right for adding a column or a table: edit the class and
restart. Write a migration instead for:

- renaming a column or a table: transform sees a dropped column and a new one, and the
  data stays behind;
- changing data: a backfill, moving values between columns, splitting a table;
- an index on an existing table, above all a unique one that existing rows might break;
- anything involving triggers. Transform rebuilds a table as `<table>_new_<hex>` and renames
  it, SQLite re-checks every trigger on that rename, and a trigger naming a table that was
  just dropped makes the rename fail half-way: the old table is gone and its rows are
  stranded in the copy (the item-9 incident; the cleanup is the DROP TRIGGER in app/db.py).

A migration is a module `NNNN_what.py` in this package with one function, `up(conn)`, where
conn is an apsw Connection; numbers are never reused or reordered. Also change the class
in app/db.py to the end state, because a new database is built from the classes alone: when
the database has no tables yet, every migration is recorded as applied without running.

Pending migrations run in order, each in its own transaction together with its row in
`schema_migrations`, so a failing one changes nothing and stops the run. They run at every
start (app/db.py, before the tables are declared) and by hand:

    uv run python -m app.migrations [--db PATH]
"""

import importlib
import pkgutil
import re
from datetime import UTC, datetime

TABLE = "schema_migrations"


def available():
    """[(version, module name)] in this package, in order."""
    found = []
    for m in pkgutil.iter_modules(__path__):
        if match := re.fullmatch(r"(\d{4})_\w+", m.name):
            found.append((int(match.group(1)), m.name))
    versions = [v for v, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"two migrations share a number: {sorted(versions)}")
    return sorted(found)


def _applied(conn):
    return {v for (v,) in conn.execute(f"SELECT version FROM {TABLE}")}


def _is_new(conn):
    (n,) = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name != ?",
        [TABLE],
    ).fetchone()
    return n == 0


def migrate(conn, steps=None):
    """Apply the pending migrations; the number applied. steps: [(version, up)] (tests)."""
    if steps is None:
        steps = [
            (v, importlib.import_module(f"{__name__}.{name}").up)
            for v, name in available()
        ]
    new = _is_new(conn)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = _applied(conn)
    ran = 0
    for version, up in steps:
        if version in done:
            continue
        with conn:  # one transaction: the step and its row, or neither
            if not new:
                up(conn)
                ran += 1
            conn.execute(
                f"INSERT INTO {TABLE} (version, applied_at) VALUES (?, ?)",
                [version, datetime.now(UTC).isoformat(timespec="seconds")],
            )
    return ran
