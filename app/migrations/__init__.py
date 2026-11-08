"""Numbered schema migrations: the only way an existing database's schema changes.

app/db.py creates a table from its class only when the table doesn't exist yet. It never
rebuilds or alters an existing one: `transform=True` did, and on every start it copied ten
tables into `<table>_new_<hex>` and renamed them back (a bool field never compares equal to
the INTEGER SQLite reads back), dropped any column the class no longer declared, and its
rename is what stranded rows in the item-9 incident (a trigger naming a dropped table).

So a new table can come from its class alone, and everything else is a migration:

- adding a column: `ALTER TABLE ... ADD COLUMN` (0012 onward show the pattern). After the
  migrations, `check` stops the app when a table lacks a column its class declares;
- renaming or removing a column or a table. A column only the table still has is kept, and
  `check` logs a warning naming it;
- changing data: a backfill, moving values between columns, splitting a table;
- an index on an existing table, above all a unique one that existing rows might break;
- anything involving triggers.

A migration is a module `NNNN_what.py` in this package with one function, `up(conn)`, where
conn is an apsw Connection; numbers are never reused or reordered. Also change the class
in app/db.py to the end state, because a new database is built from the classes alone: when
the database has no tables yet, every migration is recorded as applied without running.

Pending migrations run in order, each in its own transaction together with its row in
`schema_migrations`, so a failing one changes nothing and stops the run. They run at every
start (app/db.py, before the tables are declared, then `check`) and by hand:

    uv run python -m app.migrations [--db PATH]
"""

import importlib
import logging
import pkgutil
import re
from datetime import UTC, datetime

TABLE = "schema_migrations"
log = logging.getLogger("causematch")


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


def check(conn, tables):
    """Stop when a table lacks a column its class declares; warn about one only the table
    has (kept, never dropped). tables: {name: [column, ...]} from app/db.py's classes.

    Names only: a changed type, primary key or NOT NULL, and a table whose class is gone,
    pass unnoticed here and need a migration all the same."""
    for name, want in tables.items():
        have = [r[1] for r in conn.execute(f"PRAGMA table_info([{name}])")]
        if missing := [c for c in want if c not in have]:
            raise RuntimeError(
                f"table {name} has no column {', '.join(missing)}, which its class in "
                f"app/db.py declares: add a migration "
                f"app/migrations/{available()[-1][0] + 1:04d}_<what>.py that adds "
                f"{'them' if len(missing) > 1 else 'it'} "
                f"(ALTER TABLE {name} ADD COLUMN ...)"
            )
        if extra := [c for c in have if c not in want]:
            log.warning(
                "table %s keeps column(s) %s that its class in app/db.py lacks: "
                "a migration should drop or rename them",
                name,
                ", ".join(extra),
            )
