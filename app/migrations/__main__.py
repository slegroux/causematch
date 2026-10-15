"""uv run python -m app.migrations [--db PATH]: apply pending migrations, print how many.

Doesn't import app.db, which would migrate on import and declare the tables; an existing
database is required (the app creates new ones).
"""

import argparse
import sys

import apsw

from app.config import db_path
from app.migrations import migrate


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.migrations")
    p.add_argument(
        "--db", default=str(db_path()), help="SQLite file (default: %(default)s)"
    )
    path = p.parse_args(argv).db
    try:
        conn = apsw.Connection(path, flags=apsw.SQLITE_OPEN_READWRITE)
    except apsw.CantOpenError:
        print(f"no database at {path}", file=sys.stderr)
        return 1
    try:
        print(f"applied {migrate(conn)} migration(s) to {path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
